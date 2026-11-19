-- Progress presentation for AI Photo Agent analysis.
--
-- Lightroom cannot report progress for a blocked HTTP request, so the service
-- exposes analysis as a job and the plug-in polls it while showing progress.
--
-- `run` is the entry point used by Actions.lua and picks the best available
-- presentation:
--   "dialog" - LrDialogs.showModalProgressDialog: a real modal progress window
--              with a moving bar, a live caption and a working Cancel button.
--              This is what the user sees; it repaints because the dialog owns
--              its own event loop.
--   "bezel"  - fallback for older SDKs: refresh LrDialogs.showBezel every poll.
--              Less visible, but never blocks and always available.

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrTasks = import "LrTasks"

local ProgressWindow = {}

ProgressWindow.STAGE_TITLES = {
    queued = "排队中",
    measuring = "读取与量化照片",
    provider = "模型分析中",
    validating = "校验并保存建议",
    succeeded = "分析完成",
    failed = "分析失败",
    cancelled = "已取消",
}

ProgressWindow.STAGE_PROGRESS = {
    queued = 0.08,
    measuring = 0.28,
    provider = 0.62,
    validating = 0.9,
}

ProgressWindow.TERMINAL_STAGES = {
    succeeded = true,
    failed = true,
    cancelled = true,
}

ProgressWindow.DIALOG_TITLE = "AI Photo Agent 正在分析照片"
ProgressWindow.CANCEL_MESSAGE = "已取消分析。"

local function clamp(value, low, high)
    if value < low then
        return low
    end
    if value > high then
        return high
    end
    return value
end

-- LrDialogs.showBezel rejects a fade delay outside 0..10 seconds, so every
-- bezel in this module goes through the same clamp.
local BEZEL_MAX_SECONDS = 10

local function show_bezel(message, seconds)
    LrDialogs.showBezel(message, clamp(tonumber(seconds) or 2, 0, BEZEL_MAX_SECONDS))
end

function ProgressWindow.is_terminal(status)
    return type(status) == "table" and ProgressWindow.TERMINAL_STAGES[status.stage] == true
end

-- Progress creeps forward inside the provider stage so the bar never looks
-- frozen during a long model call, but it never completes on its own.
function ProgressWindow.estimate_progress(stage, elapsed_seconds)
    local base = ProgressWindow.STAGE_PROGRESS[stage] or 0.05
    if stage == "provider" then
        local elapsed = tonumber(elapsed_seconds) or 0
        return clamp(base + 0.24 * (1 - math.exp(-elapsed / 45)), 0, 0.98)
    end
    return clamp(base, 0, 0.98)
end

function ProgressWindow.format_elapsed(seconds)
    local total = math.floor(tonumber(seconds) or 0)
    local minutes = math.floor(total / 60)
    local remainder = total - minutes * 60
    if minutes > 0 then
        return string.format("%d 分 %02d 秒", minutes, remainder)
    end
    return string.format("%d 秒", total)
end

-- One line describing the current stage, reused by the dialog and the bezel.
function ProgressWindow.describe(status)
    local stage = type(status) == "table" and status.stage or "queued"
    local title = ProgressWindow.STAGE_TITLES[stage] or tostring(stage)
    local hint = type(status) == "table" and status.progress_hint or nil
    local elapsed = ProgressWindow.format_elapsed(
        type(status) == "table" and status.elapsed_seconds or 0
    )
    if type(hint) == "string" and #hint > 0 then
        return string.format("%s — %s（已用 %s）", title, hint, elapsed)
    end
    return string.format("%s（已用 %s）", title, elapsed)
end

local function new_state()
    return {
        stopped = false,
        done = false,
        cancelled = false,
        timed_out = false,
        last_status = nil,
        last_error = nil,
        missing = false,
        waited = 0,
    }
end

local function poll_once(state, options)
    local status, error_message = options.api.get_status(options.path)
    if status then
        state.last_status = status
        state.last_error = nil
        if ProgressWindow.is_terminal(status) then
            state.done = true
            state.stopped = true
        end
    else
        state.last_error = error_message
        -- A missing job (service restarted) ends the wait; a transient network
        -- error does not, because the job keeps running server-side.
        if type(error_message) == "string" and error_message:find("404", 1, true) then
            state.missing = true
            state.stopped = true
        end
    end
end

local function caption_for(state)
    if state.cancelled then
        return "已取消"
    end
    if state.last_status then
        return ProgressWindow.describe(state.last_status)
    end
    if state.last_error then
        return tostring(state.last_error) .. "（继续等待）"
    end
    return "已提交分析任务，正在等待服务端…"
end

local function finish(state, options)
    -- Called directly, not through pcall: the callback may yield, and a yield
    -- inside pcall aborts with "Yielding is not allowed within a C or
    -- metamethod call" in Lua 5.1.
    if state.last_status and type(options.on_update) == "function" then
        options.on_update(state.last_status)
    end
    if state.done then
        return state.last_status
    end
    if state.cancelled then
        return nil, ProgressWindow.CANCEL_MESSAGE
    end
    if state.missing then
        return nil, "服务端已不再持有该分析任务，请重试。"
    end
    return nil, state.last_error or "等待分析超时。"
end

-- Deliver a server-side cancel (user cancel or timeout), retrying once: cancel
-- must reach the service, otherwise the abandoned analysis keeps running and the
-- next click looks queued behind it.
local function cancel_server_job(api, path, log, reason)
    local notified, notify_error = api.cancel(path)
    if not notified then
        log("progress: " .. tostring(reason) .. " cancel failed ("
            .. tostring(notify_error) .. "), retrying once")
        LrTasks.sleep(1)
        notified, notify_error = api.cancel(path)
    end
    if notified then
        log("progress: cancel acknowledged by the service (" .. tostring(reason) .. ")")
    else
        log("progress: cancel could not be delivered: " .. tostring(notify_error))
    end
end


-- Bezel fallback: short transient message refreshed on every poll.
function ProgressWindow.wait_for_job(options)
    local api = assert(options.api, "ProgressWindow.wait_for_job needs api")
    local path = assert(options.path, "ProgressWindow.wait_for_job needs path")
    local poll_seconds = options.poll_seconds or 1
    local timeout_seconds = options.timeout_seconds or 600
    local min_visible = options.min_visible_seconds or 1.2

    local state = new_state()
    state.waited = 0

    local function current_text()
        return "AI Photo Agent：" .. caption_for(state)
    end

    show_bezel(current_text(), 2)

    while not state.stopped and state.waited < timeout_seconds do
        LrTasks.sleep(poll_seconds)
        state.waited = state.waited + poll_seconds
        poll_once(state, options)
        if not state.stopped then
            show_bezel(current_text(), poll_seconds + 3)
        end
    end

    state.timed_out = (not state.done and not state.cancelled and not state.missing)
    if state.done then
        show_bezel(current_text(), min_visible)
    elseif state.cancelled then
        show_bezel("AI Photo Agent：已取消", min_visible)
    elseif state.missing then
        show_bezel("AI Photo Agent：服务端已不再持有该分析任务。", min_visible)
    else
        show_bezel("AI Photo Agent：等待分析超时。", min_visible)
    end

    if state.timed_out then
        -- A timeout is abandonment too: cancel the server job instead of
        -- leaving it running behind the concurrency limit.
        state.last_error = "等待分析超时。"
        cancel_server_job(api, path, options.log or function() end, "timeout")
    end

    return finish(state, options)
end

-- Preferred presentation: a modal progress dialog that keeps its own event loop
-- alive, so the caption and the bar keep repainting while the plug-in waits.
--
-- The dialog body is NOT wrapped in pcall: LrTasks.sleep / LrHttp.* yield, and a
-- yield may not cross the C-call boundary that pcall introduces (Lua 5.1 aborts
-- with "Yielding is not allowed within a C or metamethod call"). Error reporting
-- comes from LrDialogs.attachErrorDialogToFunctionContext in the menu entries.
function ProgressWindow.run(options)
    if type(LrDialogs.showModalProgressDialog) ~= "function" then
        return ProgressWindow.wait_for_job(options)
    end

    local api = assert(options.api, "ProgressWindow.run needs api")
    local path = assert(options.path, "ProgressWindow.run needs path")

    local poll_seconds = options.poll_seconds or 1
    local timeout_seconds = options.timeout_seconds or 600
    local min_visible = options.min_visible_seconds or 1.2
    local log = options.log or function() end

    local state = new_state()

    LrFunctionContext.callWithContext("AIPhotoAgentProgress", function(context)
        local scope = LrDialogs.showModalProgressDialog {
            title = ProgressWindow.DIALOG_TITLE,
            caption = "正在提交分析任务…",
            width = 420,
            cannotCancel = false,
            functionContext = context,
        }
        if not scope then
            error("showModalProgressDialog returned no progress scope")
        end

        scope:setCaption(caption_for(state))

        while not state.stopped and state.waited < timeout_seconds do
            LrTasks.sleep(poll_seconds)
            state.waited = state.waited + poll_seconds

            if scope:isCanceled() then
                state.cancelled = true
                state.stopped = true
                break
            end

            poll_once(state, options)
            local status = state.last_status
            if status and scope.setPortionComplete then
                scope:setPortionComplete(
                    ProgressWindow.estimate_progress(status.stage, status.elapsed_seconds),
                    1
                )
            end
            scope:setCaption(caption_for(state))
        end

        if state.done and not state.cancelled then
            if scope.setPortionComplete then
                scope:setPortionComplete(1, 1)
            end
            scope:setCaption(caption_for(state))
            LrTasks.sleep(math.max(min_visible - poll_seconds, 0))
        end

        scope:done()
    end)

    state.timed_out = (not state.done and not state.cancelled and not state.missing)
    if state.cancelled then
        -- A normal HTTP call, so it runs after the dialog is gone: doing it
        -- inside the dialog body would skip scope:done() if the request raised.
        cancel_server_job(api, path, log, "user cancel")
    elseif state.timed_out then
        -- 1.7.2: a timeout used to leave the job running server-side forever.
        state.last_error = "等待分析超时。"
        cancel_server_job(api, path, log, "timeout")
    end

    return finish(state, options)
end

return ProgressWindow
