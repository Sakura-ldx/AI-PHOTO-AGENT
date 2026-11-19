-- Single review window for the AI Photo Agent plug-in.
--
-- Everything that used to be three separate steps after the progress window
-- (a message box with the analysis, a modal style picker, then a confirmation
-- prompt) now lives in one floating window: the analysis text, the style menu,
-- the intensity slider, a preview button, an original/edited toggle and the
-- keep/cancel buttons.
--
-- The window is a *floating* dialog (SDK 5.0+), not a modal one, because the
-- preview works by writing develop settings onto the photo: the user has to be
-- able to see the photo behind the window change, and a modal dialog would
-- block and repaint nothing.
--
-- All I/O (HTTP, catalog write access) is injected by Actions.lua and runs in
-- the polling loop below, never inside a button callback. Lua 5.1 cannot yield
-- across a C boundary, and button callbacks are invoked from the UI's nested
-- loop, so doing network work there risks "Yielding is not allowed".

local LrBinding = import "LrBinding"
local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrTasks = import "LrTasks"
local LrView = import "LrView"

local ReviewWindow = {}

ReviewWindow.WINDOW_TITLE = "AI Photo Agent - 分析与修图"
ReviewWindow.POLL_SECONDS = 0.15
-- Bumped from "AIPhotoAgentReview": a frame remembered by 1.4.0 would restore a
-- window sized for single-line (clipped) text.
ReviewWindow.FRAME_KEY = "AIPhotoAgentReview2"

-- static_text defaults to height_in_lines = 1, which renders exactly one line and
-- clips the rest at `width`. The SDK only wraps when height_in_lines is -1 *and*
-- a width is given ("LrView text properties" in the SDK reference), so every
-- long-text node below sets both.
local VIEW_WIDTH = 580
local TEXT_WIDTH = 556
local ANALYSIS_VIEW_HEIGHT = 220
local DETAIL_VIEW_HEIGHT = 140
local STATUS_LINES = 3

local BUTTON_PREVIEW = "预览效果"
local BUTTON_KEEP = "保留调整"
local BUTTON_CANCEL = "取消并还原"

local function noop()
end

function ReviewWindow.available()
    return type(LrDialogs.presentFloatingDialog) == "function"
end

--: Handle of the dialog currently on screen (set by presentFloatingDialog's
--: onShow callback). Lightroom pushes floating dialogs behind the main window
--: as soon as the user clicks the catalog, so the review panel needs a way to
--: come back: a menu command and the selection/source observers call this.
ReviewWindow._dialog = nil
ReviewWindow._log = noop
ReviewWindow._active = false
--: 1.7.0: heartbeat of the polling loop. A menu click must be able to tell a
--: window that is merely behind the main window (loop alive) from a review
--: session that died silently (loop gone). _pulse is refreshed on every poll
--: tick; a dead loop leaves it frozen.
ReviewWindow._pulse = 0
--: True while the loop is inside an injected call that can legitimately take a
--: while (the preview HTTP request and the catalog write). The heartbeat cannot
--: move during that call, so liveness checks treat a recent busy flag as alive.
ReviewWindow._busy = false
ReviewWindow._busy_since = 0
--: Set by the "显示分析窗口" menu entry when the existing window cannot be
--: raised; the polling loop in run() then presents a fresh window on the same
--: state, which is the only reliable way back when Lightroom withholds a handle.
--: _recall_seq/_recall_ack let the menu verify that the loop actually acted on
--: the request instead of trusting a flag nobody consumes.
ReviewWindow._recall = false
ReviewWindow._recall_seq = 0
ReviewWindow._recall_ack = 0
--: Set by the selection/source observers; the polling loop turns it into a
--: toFront() call. Observers themselves never touch the dialog (1.6.4).
ReviewWindow._wake = false

--: A live loop ticks every POLL_SECONDS; 15 seconds is long enough for a busy
--: UI thread and still short enough to detect a session that was torn down.
ReviewWindow.LIVENESS_SECONDS = 15
--: The preview HTTP request is local parameter math and normally returns in a
--: second. 1.7.2: the limit must exceed the develop request timeout (600 s in
--: Config.lua), otherwise a slow-but-healthy preview counted as dead and a menu
--: recall released the session while its loop was still writing settings.
ReviewWindow.BUSY_LIMIT_SECONDS = 660

local function log(message)
    ReviewWindow._log(tostring(message))
end

function ReviewWindow._touch()
    ReviewWindow._pulse = os.time()
end

function ReviewWindow.is_open()
    return ReviewWindow._dialog ~= nil
end

function ReviewWindow.is_active()
    return ReviewWindow._active == true
end

function ReviewWindow.is_busy_alive()
    if ReviewWindow._active ~= true or ReviewWindow._busy ~= true then
        return false
    end
    local since = tonumber(ReviewWindow._busy_since) or 0
    return (os.time() - since) <= ReviewWindow.BUSY_LIMIT_SECONDS
end

function ReviewWindow.is_live()
    if ReviewWindow._active ~= true then
        return false
    end
    if ReviewWindow.is_busy_alive() then
        return true
    end
    local pulse = tonumber(ReviewWindow._pulse) or 0
    return pulse > 0 and (os.time() - pulse) <= ReviewWindow.LIVENESS_SECONDS
end

--: Ask the running loop to close the current window and present a fresh one on
--: the same state. Returns the sequence number the loop must acknowledge with
--: recall_acknowledged(); the menu uses that to fall back to a rebuild when the
--: loop is gone (a boolean flag alone silently did nothing).
function ReviewWindow.request_recall()
    if ReviewWindow._active ~= true then
        return false, "inactive"
    end
    ReviewWindow._recall_seq = (ReviewWindow._recall_seq or 0) + 1
    ReviewWindow._recall = true
    return true, ReviewWindow._recall_seq
end

function ReviewWindow.recall_acknowledged(seq)
    local wanted = tonumber(seq) or 0
    return (tonumber(ReviewWindow._recall_ack) or 0) >= wanted
end

--: Drop an in-memory session whose loop no longer answers. The orphan window
--: (if any) is closed first: leaving it on screen would give the user a window
--: whose buttons nobody polls any more.
function ReviewWindow.release_stale()
    local api = ReviewWindow._dialog
    ReviewWindow._dialog = nil
    ReviewWindow._active = false
    ReviewWindow._pulse = 0
    ReviewWindow._busy = false
    ReviewWindow._busy_since = 0
    ReviewWindow._recall = false
    ReviewWindow._wake = false
    if api and type(api.close) == "function" then
        local closed, close_error = pcall(api.close)
        if not closed then
            log("review: closing the stale dialog failed: " .. tostring(close_error))
        end
    end
    log("review: released a stale review session")
end

function ReviewWindow.bring_to_front()
    local api = ReviewWindow._dialog
    if not api or type(api.toFront) ~= "function" then
        return false
    end
    -- toFront() on a handle Lightroom already destroyed raises; swallowing the
    -- error here keeps the review session alive and lets the loop re-present.
    local raised, raise_error = pcall(api.toFront)
    if not raised then
        log("review: toFront failed: " .. tostring(raise_error))
        ReviewWindow._dialog = nil
        return false
    end
    return true
end

local function style_items(styles)
    local items = {}
    local names = {}
    local descriptions = {}
    for _, style in ipairs(styles) do
        local style_id = tostring(style.id)
        items[#items + 1] = {
            title = tostring(style.name or style.id),
            value = style_id,
        }
        names[style_id] = tostring(style.name or style.id)
        descriptions[style_id] = tostring(
            style.description or style.best_fit_reason or ""
        )
    end
    return items, names, descriptions
end

local function key_of(style_id, intensity)
    return tostring(style_id) .. "|" .. tostring(intensity)
end

local function split_lines(text)
    local lines = {}
    for line in (tostring(text or "") .. "\n"):gmatch("(.-)\n") do
        lines[#lines + 1] = line
    end
    return lines
end

-- One static_text per line: the analysis text already carries its own paragraph
-- structure ("总体判断：" / "技术观察：" …). Per-line nodes keep those headings on
-- their own rows while each row still wraps, instead of collapsing everything
-- into one clipped line.
local function paragraph_nodes(factory, text)
    local nodes = {}
    local has_text = false
    for _, line in ipairs(split_lines(text)) do
        if line == "" then
            nodes[#nodes + 1] = factory:static_text {
                title = " ",
                width = TEXT_WIDTH,
            }
        else
            has_text = true
            nodes[#nodes + 1] = factory:static_text {
                title = line,
                width = TEXT_WIDTH,
                height_in_lines = -1,
            }
        end
    end
    if not has_text then
        return {
            factory:static_text {
                title = "（没有返回分析文本）",
                width = TEXT_WIDTH,
            },
        }
    end
    return nodes
end


function ReviewWindow.run(options)
    local log = options.log or noop
    local styles = options.styles

    if type(styles) ~= "table" or #styles == 0 then
        return nil, "分析结果没有返回可用风格。"
    end
    if not ReviewWindow.available() then
        return nil, "这个 Lightroom 版本没有浮动窗口接口。"
    end
    if ReviewWindow._active then
        if ReviewWindow.is_live() or ReviewWindow.is_busy_alive() then
            return nil, "已经有一个分析窗口打开了。"
        end
        -- 1.7.0: a loop that died without running its cleanup handler used to
        -- leave _active stuck forever, so every later run() answered "已经有一个
        -- 分析窗口打开了。" and the menu could never rebuild. Release it instead.
        log("review: releasing a stale session before starting a new one")
        ReviewWindow.release_stale()
    end
    ReviewWindow._active = true
    -- Flags left over from a previous session must not fire immediately.
    ReviewWindow._recall = false
    ReviewWindow._recall_seq = 0
    ReviewWindow._recall_ack = 0
    ReviewWindow._wake = false
    ReviewWindow._busy = false
    ReviewWindow._busy_since = 0
    ReviewWindow._touch()

    local items, names, descriptions = style_items(styles)

    local state = {
        request = nil,
        closed = false,
        decision = nil,
        development = nil,
        applied_key = nil,
    }

    local decision = LrFunctionContext.callWithContext(
        "AIPhotoAgentReview",
        function(context)
            context:addCleanupHandler(function()
                -- Runs on normal exit AND when an exception unwinds the session
                -- (for example a catalog write failing mid-preview). If the log
                -- shows this line without a "loop ended" line before it, the
                -- session died from an error rather than from a button press.
                log("review: context cleanup")
                ReviewWindow._active = false
                ReviewWindow._dialog = nil
                ReviewWindow._pulse = 0
                ReviewWindow._busy = false
                ReviewWindow._busy_since = 0
            end)

            local props = LrBinding.makePropertyTable(context)
            props.styleId = tostring(styles[1].id)
            props.intensity = 100
            props.status = "点「预览效果」把参数试用到这张照片上；窗口被主界面挡住了，就点菜单 文件 > 增效工具附加功能 > AI Photo Agent - 显示分析窗口，它会重新弹出来。"
            props.detail = ""

            local function current_style_id()
                return tostring(props.styleId)
            end

            local function current_intensity()
                return (tonumber(props.intensity) or 0) / 100
            end

            local function percent(value)
                return math.floor(value * 100 + 0.5)
            end

            local function do_preview()
                local style_id = current_style_id()
                local intensity = current_intensity()
                local wanted = key_of(style_id, intensity)
                if state.development and state.applied_key == wanted then
                    props.status = "照片上已经是这套参数了。按 \\ 对比修改前后。"
                    return true
                end

                props.status = string.format(
                    "正在生成「%s」的参数…",
                    tostring(names[style_id] or style_id)
                )
                local development, fetch_error = options.fetch_development(
                    style_id,
                    intensity
                )
                if not development then
                    props.status = "预览失败：" .. tostring(fetch_error)
                    log("review: fetch failed: " .. tostring(fetch_error))
                    return false
                end

                local applied, apply_error = options.apply_preview(development)
                if not applied then
                    props.status = "预览失败：" .. tostring(apply_error)
                    log("review: apply failed: " .. tostring(apply_error))
                    return false
                end

                state.development = development
                state.applied_key = wanted
                props.detail = options.parameter_summary(development)
                props.status = string.format(
                    "已把「%s」试用到照片上（强度 %d%%）。按 \\ 对比修改前后。",
                    tostring(development.style_name or style_id),
                    percent(intensity)
                )
                log("review: preview applied for " .. style_id)
                return true
            end

            -- 1.6.2: the in-window compare button is gone. It rewrote develop settings
            -- from inside the polling loop; when that write threw, the whole
            -- review session unwound and the window vanished. Lightroom's own
            -- before/after (backslash) already compares against the
            -- "AI Photo Agent - Before" snapshot, so nothing is lost.

            local factory = LrView.osFactory()
            local function build_contents()
                return factory:column {
                bind_to_object = props,
                spacing = 10,
                width = VIEW_WIDTH,
                factory:scrolled_view {
                    width = VIEW_WIDTH,
                    height = ANALYSIS_VIEW_HEIGHT,
                    -- LrView factories take ONE props table; passing the
                    -- paragraph nodes as variadic arguments silently built an
                    -- empty container on the real SDK (1.7.2).
                    factory:column {
                        unpack(paragraph_nodes(factory, options.analysis_text)),
                    },
                },
                factory:separator { fill_horizontal = 1 },
                factory:row {
                    spacing = 8,
                    factory:static_text { title = "修图风格", width = 70 },
                    factory:popup_menu {
                        items = items,
                        value = LrView.bind("styleId"),
                        width = 300,
                    },
                },
                factory:row {
                    spacing = 8,
                    factory:static_text { title = "强度", width = 70 },
                    factory:slider {
                        value = LrView.bind("intensity"),
                        min = 0,
                        max = 100,
                        integral = true,
                        width = 300,
                    },
                    factory:static_text {
                        title = LrView.bind({
                            key = "intensity",
                            transform = function(value)
                                return string.format("%d%%", value or 0)
                            end,
                        }),
                    },
                },
                factory:static_text {
                    title = LrView.bind({
                        key = "styleId",
                        transform = function(value)
                            return "当前风格：" .. tostring(
                                descriptions[tostring(value)] or ""
                            )
                        end,
                    }),
                    width = TEXT_WIDTH,
                    height_in_lines = 2,
                },
                factory:row {
                    spacing = 8,
                    factory:push_button {
                        title = BUTTON_PREVIEW,
                        action = function()
                            state.request = "preview"
                        end,
                    },
                    factory:push_button {
                        title = BUTTON_KEEP,
                        action = function()
                            state.request = "keep"
                        end,
                    },
                    factory:push_button {
                        title = BUTTON_CANCEL,
                        action = function()
                            state.request = "cancel"
                        end,
                    },
                },
                factory:static_text {
                    title = LrView.bind("status"),
                    width = TEXT_WIDTH,
                    height_in_lines = STATUS_LINES,
                },
                factory:scrolled_view {
                    width = VIEW_WIDTH,
                    height = DETAIL_VIEW_HEIGHT,
                    factory:static_text {
                        title = LrView.bind("detail"),
                        width = TEXT_WIDTH,
                        height_in_lines = -1,
                    },
                },
                }
            end

            local function present()
                ReviewWindow._log = log
                ReviewWindow._wake = false
                ReviewWindow._touch()
                log("review: presenting dialog with blockTask=false")
                local previous = ReviewWindow._dialog
                if previous and type(previous.close) == "function" then
                    log("review: closing the previous dialog handle")
                    -- Closing the old window fires windowWillClose; that must not
                    -- end the review session we are about to re-present.
                    state.suppress_close = true
                    ReviewWindow._dialog = nil
                    local closed, close_error = pcall(previous.close)
                    if not closed then
                        log("review: closing the previous dialog failed: " .. tostring(close_error))
                    end
                end
                LrDialogs.presentFloatingDialog(_PLUGIN, {
                    title = ReviewWindow.WINDOW_TITLE,
                    contents = build_contents(),
                    -- false: the dialog must not block this task, otherwise the
                    -- polling loop below could never run the HTTP calls.
                    blockTask = false,
                    save_frame = ReviewWindow.FRAME_KEY,
                    onShow = function(api)
                        ReviewWindow._dialog = api
                        ReviewWindow._touch()
                        if api then
                            log(
                                "review: onShow fired, toFront="
                                    .. tostring(type(api.toFront))
                                    .. " close="
                                    .. tostring(type(api.close))
                            )
                        else
                            log("review: onShow fired without a handle")
                        end
                        if api and type(api.toFront) == "function" then
                            local raised, raise_error = pcall(api.toFront)
                            if not raised then
                                log("review: onShow toFront failed: " .. tostring(raise_error))
                            end
                        end
                    end,
                    windowWillClose = function()
                        -- The handle is dead from here on; never touch it again.
                        ReviewWindow._dialog = nil
                        if state.suppress_close then
                            -- We are swapping this window for a fresh one on the
                            -- same state; the review session must stay alive.
                            state.suppress_close = false
                            log("review: previous dialog closed for a re-present")
                        else
                            log("review: window closed")
                            state.closed = true
                        end
                    end,
                    -- 1.6.4: observers must not touch the dialog. They run in
                    -- Lightroom's UI context, and an exception thrown there (for
                    -- example toFront() on a handle Lightroom already destroyed)
                    -- unwinds the review session without a single log line --
                    -- exactly the "窗口还挂在任务栏上但再也叫不回来" failure. A
                    -- boolean assignment cannot throw, so the polling loop below
                    -- does the raising instead.
                    selectionChangeObserver = function()
                        ReviewWindow._wake = true
                    end,
                    sourceChangeObserver = function()
                        ReviewWindow._wake = true
                    end,
                })
            end

            present()

            while not state.closed and not state.decision do
                LrTasks.sleep(ReviewWindow.POLL_SECONDS)
                ReviewWindow._touch()
                -- A close can race with a pending recall: never present a new
                -- window after the user dismissed the current one.
                if state.closed then
                    break
                end
                if ReviewWindow._wake then
                    ReviewWindow._wake = false
                    if ReviewWindow.bring_to_front() then
                        log("review: raised the dialog after a selection change")
                    else
                        -- No usable handle: ask for a fresh window instead of
                        -- losing the panel until the next selection change.
                        ReviewWindow._recall = true
                        log("review: could not raise the dialog; presenting a fresh one")
                    end
                end
                if ReviewWindow._recall then
                    -- The menu item could not raise the existing window (no
                    -- handle yet, or Lightroom refused toFront). Presenting a
                    -- fresh window on the same state is what makes the panel
                    -- reachable again in that case.
                    local recall_seq = ReviewWindow._recall_seq
                    ReviewWindow._recall = false
                    log("review: recall requested, presenting the dialog again")
                    present()
                    ReviewWindow._recall_ack = recall_seq
                    log("review: recall acknowledged (seq=" .. tostring(recall_seq) .. ")")
                end
                local request = state.request
                state.request = nil
                if request then
                    -- The injected HTTP call and catalog write cannot refresh the
                    -- heartbeat while they run, so mark the loop busy first.
                    ReviewWindow._busy = true
                    ReviewWindow._busy_since = os.time()
                    ReviewWindow._touch()
                end
                if request == "preview" then
                    log("review: handling request preview")
                    do_preview()
                elseif request == "keep" then
                    log("review: handling request keep")
                    if not state.development then
                        do_preview()
                    end
                    if state.development then
                        state.decision = "keep"
                    end
                elseif request == "cancel" then
                    log("review: handling request cancel")
                    state.decision = "cancel"
                end
                if request then
                    ReviewWindow._touch()
                    ReviewWindow._busy = false
                end
            end

            log(
                string.format(
                    "review: loop ended closed=%s decision=%s",
                    tostring(state.closed),
                    tostring(state.decision)
                )
            )

            if state.decision == "keep" and state.development then
                if options.on_keep then
                    options.on_keep(
                        state.development,
                        current_style_id(),
                        current_intensity()
                    )
                end
            end

            local handle = ReviewWindow._dialog
            if handle and type(handle.close) == "function" then
                ReviewWindow._dialog = nil
                -- Closing a window the user already dismissed used to throw
                -- here, and that exception tore the whole review task down
                -- (no restore, no log). close() does not yield, so the guarded
                -- call is safe and keeps the session alive either way.
                local closed, close_error = pcall(handle.close)
                if not closed then
                    log("review: closing the dialog failed: " .. tostring(close_error))
                end
            end

            return state.decision
        end
    )

    if decision == "keep" then
        return "keep"
    end
    if decision == "cancel" then
        return "cancel"
    end
    -- 1.7.2: distinguish "the user closed the window" from a button decision so
    -- callers restore the photo instead of falling back to the modal flow.
    return nil, "closed"
end

return ReviewWindow
