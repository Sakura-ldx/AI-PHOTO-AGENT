-- Shared actions for the AI Photo Agent Lightroom Classic plug-in.

local LrApplication = import "LrApplication"
local LrApplicationView = import "LrApplicationView"
local LrBinding = import "LrBinding"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrFunctionContext = import "LrFunctionContext"
local LrLogger = import "LrLogger"
local LrPathUtils = import "LrPathUtils"
local LrTasks = import "LrTasks"
local LrView = import "LrView"

local Config = dofile(LrPathUtils.child(_PLUGIN.path, "Config.lua"))
local Http = dofile(LrPathUtils.child(_PLUGIN.path, "Http.lua"))
local Json = dofile(LrPathUtils.child(_PLUGIN.path, "Json.lua"))
local ProgressWindow = dofile(LrPathUtils.child(_PLUGIN.path, "ProgressWindow.lua"))
local ReviewWindow = dofile(LrPathUtils.child(_PLUGIN.path, "ReviewWindow.lua"))

local logger = LrLogger("AIPhotoAgent")
logger:enable("logfile")

local Actions = {
    Config = Config,
}

-- 1.7.2: one analysis at a time. Two fast menu clicks used to pass the
-- review-window guard and race over the same review state and Before snapshot.
-- The flag is cleared by an LrTasks.pcall wrapper so an exception can never
-- leave the plug-in permanently busy.
local analysis_in_progress = false

local function log(message)
    logger:info(tostring(message))
end

-- LrDialogs.showBezel rejects a fade delay outside 0..10 seconds and raises
-- "fade delay must be a number of seconds between 0 and 10". Every bezel in the
-- plug-in goes through this clamp so a long timeout can never crash an action.
local BEZEL_MAX_SECONDS = 10

local function show_bezel(message, seconds)
    local delay = tonumber(seconds) or 2
    if delay < 0 then
        delay = 0
    elseif delay > BEZEL_MAX_SECONDS then
        delay = BEZEL_MAX_SECONDS
    end
    LrDialogs.showBezel(message, delay)
end

local function debug_log_paths()
    local paths = {}
    local plugin_root = LrPathUtils.parent(LrPathUtils.parent(_PLUGIN.path))
    if plugin_root then
        paths[#paths + 1] = LrPathUtils.child(
            LrPathUtils.child(plugin_root, Config.BRIDGE_DIR_NAME),
            "debug.log"
        )
    end
    local temp_dir = LrPathUtils.getStandardFilePath("temp")
    if temp_dir then
        paths[#paths + 1] = LrPathUtils.child(temp_dir, "aipa-debug.log")
    end
    return paths
end

local function debug_log(message)
    if not Config.DEBUG then
        return
    end
    local line = tostring(os.time()) .. " " .. tostring(message) .. "\n"
    for _, path in ipairs(debug_log_paths()) do
        local ok, file = pcall(io.open, path, "a")
        if ok and file then
            pcall(function()
                file:write(line)
                file:close()
            end)
        end
    end
end

function Actions.debug_log(message)
    debug_log(message)
end

function Actions.log_info(message)
    log(message)
end

local function join_items(items, separator)
    if type(items) ~= "table" then
        return ""
    end
    local values = {}
    for _, item in ipairs(items) do
        values[#values + 1] = tostring(item)
    end
    return table.concat(values, separator or "；")
end

-- Truncate to a maximum number of bytes without cutting a UTF-8 character in
-- half: string.sub() on a byte count turned the last Chinese character into an
-- invalid sequence that some Lightroom text controls rendered as mojibake.
local function utf8_safe_truncate(text, max_bytes, suffix)
    if #text <= max_bytes then
        return text
    end
    local cut = max_bytes
    while cut > 0 do
        local next_byte = text:byte(cut + 1)
        if not next_byte or next_byte < 0x80 or next_byte >= 0xC0 then
            break
        end
        cut = cut - 1
    end
    return text:sub(1, cut) .. (suffix or "\n…")
end

local function format_analysis(analysis)
    local lines = {}
    lines[#lines + 1] = "总体判断：" .. tostring(analysis.overall_summary or "")
    lines[#lines + 1] = ""
    if type(analysis.technical_diagnosis) == "table" then
        lines[#lines + 1] = "技术观察：" .. join_items(analysis.technical_diagnosis, "；")
        lines[#lines + 1] = ""
    end
    if type(analysis.composition_observations) == "table" then
        lines[#lines + 1] = "构图观察：" .. join_items(analysis.composition_observations, "；")
        lines[#lines + 1] = ""
    end
    if type(analysis.color_tone_observations) == "table" then
        lines[#lines + 1] = "色彩影调：" .. join_items(analysis.color_tone_observations, "；")
        lines[#lines + 1] = ""
    end
    if type(analysis.recommended_styles) == "table" then
        for index, style in ipairs(analysis.recommended_styles) do
            lines[#lines + 1] = string.format(
                "%d. %s：%s",
                index,
                tostring(style.name or ""),
                tostring(style.best_fit_reason or style.description or "")
            )
        end
    end
    return utf8_safe_truncate(table.concat(lines, "\n"), 4000, "\n…")
end

local function show_error(message)
    log("ERROR " .. tostring(message))
    LrDialogs.message("AI Photo Agent", tostring(message), "critical")
end

function Actions.show_error(message)
    show_error(message)
end

local function get_target_photo()
    local catalog = LrApplication.activeCatalog()
    if not catalog then
        return nil, "没有活动的 Lightroom 目录。"
    end
    local photo = catalog:getTargetPhoto()
    if not photo then
        return nil, "请先在 Lightroom 中选中一张照片。"
    end
    if photo:getRawMetadata("isVideo") then
        return nil, "AI Photo Agent 目前只处理照片，不处理视频。"
    end
    return photo
end

local function wait_for_thumbnail(photo, size)
    local done = false
    local jpeg_data = nil
    local error_message = nil
    local request = photo:requestJpegThumbnail(
        size,
        size,
        function(jpeg, message)
            jpeg_data = jpeg
            error_message = message
            done = true
        end
    )

    local waited = 0
    while not done and waited < 60 do
        LrTasks.sleep(0.1)
        waited = waited + 0.1
    end
    -- Keep the request object alive until the callback has completed.
    local _ = request
    if not done then
        return nil, "读取照片预览超时。"
    end
    if not jpeg_data then
        return nil, error_message or "无法读取照片预览。"
    end
    return jpeg_data
end

-- getRawMetadata only accepts the keys listed in the SDK; the file path key is
-- "path" (there is no "pathname"). fileSize and fileFormat are raw metadata too.
local function photo_path(photo)
    local path = photo:getRawMetadata("path")
    if type(path) == "string" and path ~= "" then
        return path
    end
    return nil
end

local function photo_label(photo)
    return photo_path(photo) or "<未知照片>"
end

local function request_thumbnail(photo)
    show_bezel("AI Photo Agent：正在读取照片预览…", 4)
    local jpeg_data, error_message = wait_for_thumbnail(photo, Config.THUMBNAIL_SIZE)
    if jpeg_data then
        return jpeg_data
    end
    -- Lightroom answers "error loading thumb" when it cannot render a preview
    -- for this photo (16-bit TIFF, damaged preview cache, offline file). A
    -- smaller request often succeeds, so try that before giving up.
    log(string.format(
        "analyze: thumbnail unavailable (%s) for %s",
        tostring(error_message),
        photo_label(photo)
    ))
    if Config.THUMBNAIL_RETRY_SIZE ~= Config.THUMBNAIL_SIZE then
        jpeg_data, error_message = wait_for_thumbnail(photo, Config.THUMBNAIL_RETRY_SIZE)
        if jpeg_data then
            log(string.format(
                "analyze: reduced thumbnail %dpx worked for %s",
                Config.THUMBNAIL_RETRY_SIZE,
                photo_label(photo)
            ))
            return jpeg_data
        end
    end
    return nil, error_message or "无法读取照片预览。"
end

local ORIGINAL_CONTENT_TYPES = {
    jpg = "image/jpeg",
    jpeg = "image/jpeg",
    png = "image/png",
    tif = "image/tiff",
    tiff = "image/tiff",
    cr3 = "image/x-canon-cr3",
}

-- Fallback for photos Lightroom cannot preview: upload the original file and
-- let the service do the format handling (it sniffs the container, and for CR3
-- it extracts the embedded preview itself).
local function original_image_file(photo)
    local path = photo_path(photo)
    if not path then
        return nil, "Lightroom 没有报告这张照片的文件路径（path）。"
    end
    local extension = string.lower(tostring(path:match("%.([^.\\/]+)$") or ""))
    local content_type = ORIGINAL_CONTENT_TYPES[extension]
    if not content_type then
        local file_format = photo:getRawMetadata("fileFormat")
        return nil, string.format(
            "该文件是 %s（.%s），原文件上传只支持 JPEG / PNG / TIFF / CR3。",
            tostring(file_format or "未知格式"),
            extension ~= "" and extension or "?"
        )
    end
    local size = photo:getRawMetadata("fileSize")
    if type(size) ~= "number" or size <= 0 then
        local attributes = LrFileUtils.fileAttributes(path)
        size = attributes and attributes.fileSize or 0
    end
    if type(size) == "number" and size > Config.MAX_UPLOAD_BYTES then
        return nil, string.format(
            "原文件 %.0f MB，超过 %.0f MB 的上传上限。",
            size / 1048576,
            Config.MAX_UPLOAD_BYTES / 1048576
        )
    end
    local data, read_error = LrFileUtils.readFile(path)
    if not data then
        return nil, "读取原文件失败：" .. tostring(read_error or path)
    end
    local file_name = path:match("([^\\/]+)$") or ("lightroom-original." .. extension)
    return data, file_name, content_type
end

-- ---------------------------------------------------------------------------
-- Local service bootstrap
-- ---------------------------------------------------------------------------
-- Clicking "分析并修图" is the first thing a user does after starting
-- Lightroom, and the FastAPI service is usually not running yet. Instead of
-- failing with "请求失败" we launch it here and wait until it answers.

local function project_directory()
    -- The plug-in is normally loaded from <project>/lightroom/AIPhotoAgent.lrplugin,
    -- so the project root is two levels up. When it is loaded from the installed
    -- Modules copy that derivation points at the Lightroom folder instead, hence
    -- the existence check before trusting it.
    local plugin_root = LrPathUtils.parent(LrPathUtils.parent(_PLUGIN.path))
    if plugin_root and LrFileUtils.exists(LrPathUtils.child(plugin_root, "run_app.py")) == "file" then
        return plugin_root
    end
    -- SERVICE_PROJECT_DIR is an optional local override (normally set in the
    -- git-ignored Config.local.lua). Only trust it when the project is actually
    -- there; otherwise report it clearly instead of launching PowerShell
    -- against a directory that does not exist.
    local configured = Config.SERVICE_PROJECT_DIR
    if type(configured) == "string" and configured ~= ""
        and LrFileUtils.exists(LrPathUtils.child(configured, "run_app.py")) == "file" then
        return configured
    end
    return nil
end

local function python_executable(project_dir)
    local candidate = LrPathUtils.child(project_dir, ".venv")
    candidate = LrPathUtils.child(candidate, "Scripts")
    candidate = LrPathUtils.child(candidate, "python.exe")
    if LrFileUtils.exists(candidate) == "file" then
        return candidate
    end
    return "python.exe"
end

local function service_port()
    local port = string.match(tostring(Config.SERVER_URL), ":(%d+)")
    return port or "8000"
end

-- Quote a value for the PowerShell command line nested inside the cmd.exe
-- command LrTasks.execute runs. A project path is user-controlled, so it must
-- never be interpolated bare: a quote breaks the command and &|<>^% can inject.
local function ps_quote(value)
    local text = tostring(value or "")
    if text == "" then
        return nil, "empty value"
    end
    if text:find('[&|<>^"%%]') or text:find("[\r\n]") then
        return nil, text
    end
    return "'" .. text:gsub("'", "''") .. "'"
end

local function service_launch_command()
    -- Detach through PowerShell on purpose: `start /B` children are torn down
    -- together with the cmd.exe that LrTasks.execute spawns, which would kill
    -- the service the moment the command returns.
    local project_dir = project_directory()
    if not project_dir then
        return nil,
            "自动启动本地服务不可用：没有找到项目目录（run_app.py）。\n"
                .. "请手动启动服务，或在 Config.local.lua 中设置 SERVICE_PROJECT_DIR。"
    end
    local python = python_executable(project_dir)
    local quoted_python, unsafe_python = ps_quote(python)
    local quoted_dir, unsafe_dir = ps_quote(project_dir)
    if not quoted_python or not quoted_dir then
        return nil,
            "自动启动服务的路径包含不安全字符，已停止："
                .. tostring(unsafe_python or unsafe_dir)
    end
    return string.format(
        'powershell -NoProfile -WindowStyle Hidden -Command "Start-Process'
            .. " -FilePath %s"
            .. " -ArgumentList 'run_app.py','--no-browser','--port','%s'"
            .. ' -WorkingDirectory %s -WindowStyle Hidden"',
        quoted_python,
        service_port(),
        quoted_dir
    )
end

local function service_reachable()
    local status = Http.get("/api/lightroom/status")
    return status ~= nil
end

-- Returns true when the service answers, or nil plus a user-facing message.
local function ensure_service()
    if service_reachable() then
        return true
    end

    if not Config.AUTO_START_SERVICE then
        return nil,
            "无法连接本地 AI Photo Agent 服务（"
                .. tostring(Config.SERVER_URL)
                .. "）。\n请先运行 run_app.py --no-browser --port "
                .. service_port()
                .. "，然后重试。"
    end

    local command, command_error = service_launch_command()
    if not command then
        log("service: cannot auto-start: " .. tostring(command_error))
        return nil, tostring(command_error)
    end
    log("service: unreachable, launching: " .. command)
    show_bezel("AI Photo Agent：本地服务未运行，正在启动…", 4)
    LrTasks.execute(command)

    local timeout = tonumber(Config.SERVICE_START_WAIT_SECONDS) or 45
    local deadline = os.time() + timeout
    local waited = 0
    while os.time() < deadline do
        LrTasks.sleep(1)
        waited = waited + 1
        if service_reachable() then
            log(string.format("service: ready after %d seconds", waited))
            show_bezel(string.format("AI Photo Agent：本地服务已启动（%d 秒）", waited), 3)
            return true
        end
        show_bezel(string.format("AI Photo Agent：正在启动本地服务…（已等待 %d 秒）", waited), 4)
    end

    log(string.format("service: launch timed out after %d seconds", timeout))
    return nil,
        string.format(
            "本地服务启动超时（等待 %d 秒）。\n请手动在项目目录运行：\n.venv\\Scripts\\python.exe run_app.py --no-browser --port %s\n\n目标地址：%s",
            timeout,
            service_port(),
            tostring(Config.SERVER_URL)
        )
end

local function analyze_with_progress(photo_id)
    -- Prefer the job API so the user can watch every stage. The synchronous
    -- endpoint stays as a fallback for an older local service.
    log("analyze: requesting job for photo " .. tostring(photo_id))
    -- Send a real JSON object: the SDK's Lua JSON encoder renders an empty table
    -- as `[]`, which a stricter service rejects with 422 and then the plug-in
    -- silently degrades to the blocking path with no progress at all.
    local job, job_error = Http.post_json_with_timeout(
        "/api/photos/" .. tostring(photo_id) .. "/analyze/jobs",
        { provider = Config.DEFAULT_PROVIDER },
        Config.STATUS_TIMEOUT
    )
    if not job or not job.task_id then
        local job_error_text = type(job_error) == "string" and job_error
            or "无法提交分析任务。"
        -- Only an older service (route missing) may fall back to the blocking
        -- endpoint. Any other failure is reported: swallowing a real 4xx/5xx
        -- error also removed the progress window without telling the user.
        local fallback_allowed = job_error_text:find("HTTP 404", 1, true)
            or job_error_text:find("HTTP 405", 1, true)
            or job_error_text:find("HTTP 501", 1, true)
        if not fallback_allowed then
            log("analyze: job API failed: " .. job_error_text)
            return nil, job_error_text
        end
        log("analyze: job API unavailable, falling back to sync analyze: " .. job_error_text)
        show_bezel("AI Photo Agent：正在分析照片（旧版服务，无进度显示）…", BEZEL_MAX_SECONDS)
        return Http.post_empty("/api/photos/" .. tostring(photo_id) .. "/analyze")
    end

    log("analyze: job " .. tostring(job.task_id) .. " created, showing progress window")
    local status, status_error = ProgressWindow.run {
        api = {
            get_status = function(path)
                -- Short timeout: a stalled poll should not freeze the progress
                -- display, and the job keeps running server-side either way.
                return Http.get_with_timeout(path, Config.STATUS_TIMEOUT)
            end,
            cancel = function(path)
                -- The cancel endpoint lives under the status path; posting to
                -- the status path itself answers 405 and the analysis keeps
                -- running, which is what made "取消" look ignored.
                return Http.post_json_with_timeout(path .. "/cancel", {}, Config.STATUS_TIMEOUT)
            end,
        },
        path = string.format(
            "/api/photos/%s/analyze/jobs/%s",
            tostring(photo_id),
            tostring(job.task_id)
        ),
        poll_seconds = Config.JOB_POLL_SECONDS,
        timeout_seconds = Config.JOB_TIMEOUT_SECONDS,
        min_visible_seconds = Config.PROGRESS_MIN_VISIBLE,
        log = log,
    }

    if not status then
        return nil, status_error
    end
    -- The progress window returns the task *status*, with the analysis nested
    -- under .analysis. Handing the status table straight to format_analysis()
    -- produced an empty "总体判断：" dialog and an empty style picker.
    if status.stage == "failed" then
        return nil, status.error or "分析失败，请稍后重试。"
    end
    if type(status.analysis) ~= "table" then
        return nil, "服务端没有返回修图建议，请重试。"
    end
    return status.analysis
end

local function upload_and_analyze(photo)
    local image_data, file_name, content_type = request_thumbnail(photo)
    if not image_data then
        local thumbnail_error = file_name
        show_bezel("AI Photo Agent：正在直接读取原文件…", BEZEL_MAX_SECONDS)
        image_data, file_name, content_type = original_image_file(photo)
        if not image_data then
            log(string.format(
                "analyze: no usable image for %s (thumbnail: %s; original: %s)",
                photo_label(photo),
                tostring(thumbnail_error),
                tostring(file_name)
            ))
            return nil, string.format(
                "读不到这张照片（%s）。Lightroom 无法生成预览：%s；原文件也不可用：%s",
                photo_label(photo),
                tostring(thumbnail_error),
                tostring(file_name)
            )
        end
        log("analyze: uploading the original file " .. tostring(file_name))
    end

    show_bezel("AI Photo Agent：正在把照片发送到本地服务…", BEZEL_MAX_SECONDS)
    local uploaded, upload_error = Http.upload_image(image_data, file_name, content_type)
    if not uploaded then
        log("analyze: upload failed: " .. tostring(upload_error))
        return nil, upload_error
    end
    log("analyze: uploaded as photo " .. tostring(uploaded.photo_id))
    local analysis, analysis_error = analyze_with_progress(uploaded.photo_id)
    if not analysis then
        log("analyze: failed: " .. tostring(analysis_error))
        return nil, analysis_error
    end
    log("analyze: succeeded")
    return uploaded.photo_id, analysis
end

local function choose_style(analysis)
    local styles = analysis.recommended_styles
    if type(styles) ~= "table" or #styles == 0 then
        return nil, "分析结果没有返回可用风格。"
    end

    local items = {}
    local descriptions = {}
    for _, style in ipairs(styles) do
        local style_id = tostring(style.id)
        items[#items + 1] = {
            title = tostring(style.name or style.id),
            value = style_id,
        }
        descriptions[style_id] = tostring(
            style.description or style.best_fit_reason or ""
        )
    end

    local selection = nil
    LrFunctionContext.callWithContext("AIPhotoAgentStylePicker", function(context)
        local factory = LrView.osFactory()
        local props = LrBinding.makePropertyTable(context)
        props.styleId = tostring(styles[1].id)
        props.intensity = 100

        local contents = factory:column {
            bind_to_object = props,
            spacing = 10,
            factory:row {
                spacing = 8,
                factory:static_text { title = "修图风格", width = 80 },
                factory:popup_menu {
                    items = items,
                    value = LrView.bind("styleId"),
                    width = 260,
                },
            },
            factory:row {
                spacing = 8,
                factory:static_text { title = "强度", width = 80 },
                factory:slider {
                    value = LrView.bind("intensity"),
                    min = 0,
                    max = 100,
                    integral = true,
                    width = 260,
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
                title = "强度 0–100（默认 100）。"
                    .. "点“预览效果”会把参数直接试用到这张照片上，"
                    .. "你可以按 \\ 对比修改前后，确认后才决定是否保留。",
                width = 420,
            },
            factory:static_text {
                title = LrView.bind({
                    key = "styleId",
                    transform = function(value)
                        return "当前风格：" .. tostring(descriptions[value] or "")
                    end,
                }),
                width = 420,
            },
        }

        local result = LrDialogs.presentModalDialog {
            title = "AI Photo Agent - 选择修图风格",
            contents = contents,
            actionVerb = "预览效果",
            cancelVerb = "取消",
        }
        if result == "ok" then
            selection = {
                style_id = props.styleId,
                intensity = props.intensity / 100,
            }
        end
    end)

    return selection
end

local function create_virtual_copy(catalog, photo, copy_name)
    -- Lightroom Classic has a long-standing selection bug when creating
    -- virtual copies from Loupe/Develop. Grid view is the reliable context.
    LrApplicationView.switchToModule("library")
    LrApplicationView.showView("grid")
    LrTasks.sleep(0.3)
    debug_log("switched to Library grid for virtual copy")

    local function attempt()
        local before = photo:getRawMetadata("virtualCopies") or {}
        debug_log("vc before count=" .. tostring(#before))
        debug_log("vc gate opening")
        catalog:withWriteAccessDo("AI Photo Agent 创建虚拟副本", function()
            debug_log("vc gate entered")
            photo:createVirtualCopy()
            debug_log("vc create returned")
        end, { timeout = 30 })
        debug_log("vc gate closed")
        local copies = photo:getRawMetadata("virtualCopies")
        debug_log("vc after count=" .. tostring(copies and #copies))
        if type(copies) == "table" and #copies > #before then
            local target = copies[#copies]
            catalog:withWriteAccessDo("AI Photo Agent 命名虚拟副本", function()
                target:setRawMetadata("copyName", copy_name)
            end, { timeout = 30 })
            return target
        end
        return nil
    end

    local target = attempt()
    if target then
        debug_log("virtual copy created directly")
        return target
    end

    -- Some Lightroom Classic builds only create/select virtual copies
    -- reliably from Library Grid view. Retry there before giving up.
    debug_log("retrying virtual copy in Library Grid view")
    LrApplicationView.switchToModule("library")
    LrApplicationView.showView("grid")
    LrTasks.sleep(0.3)
    target = attempt()
    if target then
        debug_log("virtual copy created from grid view")
    end
    return target
end

local PREVIEW_SNAPSHOT_NAME = "AI Photo Agent - Before"


local function apply_to_virtual_copy(photo, style_name, develop_settings)
    local catalog = LrApplication.activeCatalog()
    if not catalog then
        return nil, "没有活动的 Lightroom 目录。"
    end
    if type(develop_settings) ~= "table" then
        return nil, "服务端没有返回可用的 Lightroom 参数。"
    end

    debug_log("applying style " .. tostring(style_name))
    local target = photo
    if Config.USE_VIRTUAL_COPY then
        target = create_virtual_copy(
            catalog,
            photo,
            "AI Photo Agent - " .. tostring(style_name)
        )
        if not target then
            return nil, "无法创建虚拟副本。请确认照片已在本目录中。"
        end
    else
        debug_log("virtual copy disabled; applying to selected photo with Before snapshot")
    end

    catalog:withWriteAccessDo("AI Photo Agent 应用修图", function()
        -- A snapshot gives a one-click before/after toggle, in addition to the
        -- untouched original. Only create it when it is missing: rewriting an
        -- existing Before snapshot on every bridge apply destroyed the state
        -- captured by the review flow (1.7.2).
        local snapshots = target:getDevelopSnapshots()
        local has_before = false
        if type(snapshots) == "table" then
            for _, snapshot in ipairs(snapshots) do
                if type(snapshot) == "table" and snapshot.name == PREVIEW_SNAPSHOT_NAME then
                    has_before = true
                    break
                end
            end
        end
        if not has_before then
            target:createDevelopSnapshot(PREVIEW_SNAPSHOT_NAME, true)
        end
        target:applyDevelopSettings(
            develop_settings,
            "AI Photo Agent - " .. tostring(style_name),
            false
        )
    end, { timeout = 30 })

    debug_log("applyDevelopSettings succeeded")
    catalog:setSelectedPhotos(target, {})
    LrApplicationView.switchToModule("develop")
    show_bezel("AI Photo Agent 已应用：" .. tostring(style_name), 2.5)
    return target
end

-- Preview-then-confirm flow (plug-in 1.2.0).
--
-- Lightroom has no API to delete a photo or a virtual copy, so an "approximate
-- preview" cannot be thrown away afterwards, and LrView can only show images
-- that live inside the plug-in bundle. The only honest before/after is the
-- real render: the style is applied to the selected photo, the original develop
-- settings are kept in memory, and a "Before" snapshot is written first so the
-- change is always reversible.
local PARAMETER_ROWS = {
    { key = "exposure", label = "曝光", digits = 2, unit = " EV" },
    { key = "contrast", label = "对比度" },
    { key = "highlights", label = "高光" },
    { key = "shadows", label = "阴影" },
    { key = "whites", label = "白色色阶" },
    { key = "blacks", label = "黑色色阶" },
    { key = "temperature", label = "色温" },
    { key = "tint", label = "色调" },
    { key = "vibrance", label = "自然饱和度" },
    { key = "saturation", label = "饱和度" },
    { key = "clarity", label = "清晰度" },
    { key = "dehaze", label = "去朦胧" },
    { key = "texture", label = "纹理" },
}

local function format_parameter_number(value, digits)
    local number = tonumber(value)
    if not number then
        return nil
    end
    if digits then
        return string.format("%." .. tostring(digits) .. "f", number)
    end
    return string.format("%d", math.floor(number + 0.5))
end

local function tone_curve_customised(points)
    if type(points) ~= "table" then
        return false
    end
    for _, point in ipairs(points) do
        if type(point) == "table" then
            local x = tonumber(point.x) or 0
            local y = tonumber(point.y) or 0
            if math.abs(y - x) > 2 then
                return true
            end
        end
    end
    return false
end

local function color_grading_used(grading)
    if type(grading) ~= "table" then
        return false
    end
    for _, wheel in ipairs({ "shadows", "midtones", "highlights" }) do
        local entry = grading[wheel]
        if type(entry) == "table" then
            local saturation = tonumber(entry.saturation) or 0
            local luminance = tonumber(entry.luminance) or 0
            if saturation ~= 0 or luminance ~= 0 then
                return true
            end
        end
    end
    return false
end

local function hsl_channel_count(hsl)
    if type(hsl) ~= "table" then
        return 0
    end
    local touched = 0
    for _, adjustment in pairs(hsl) do
        if type(adjustment) == "table" then
            local changed = false
            for _, axis in ipairs({ "hue", "saturation", "luminance" }) do
                if (tonumber(adjustment[axis]) or 0) ~= 0 then
                    changed = true
                    break
                end
            end
            if changed then
                touched = touched + 1
            end
        end
    end
    return touched
end

local function format_parameters(parameters)
    if type(parameters) ~= "table" then
        return "服务端未返回参数明细。"
    end

    local parts = {}
    for _, row in ipairs(PARAMETER_ROWS) do
        local number = tonumber(parameters[row.key])
        if number and number ~= 0 then
            parts[#parts + 1] = string.format(
                "%s %s%s",
                row.label,
                format_parameter_number(number, row.digits),
                row.unit or ""
            )
        end
    end

    local sharpening = parameters.sharpening
    if type(sharpening) == "table" then
        local amount = tonumber(sharpening.amount) or 0
        if amount ~= 0 then
            parts[#parts + 1] = string.format("锐化 %d", math.floor(amount + 0.5))
        end
    end

    local noise = parameters.noise_reduction
    if type(noise) == "table" then
        local luminance = math.floor((tonumber(noise.luminance) or 0) + 0.5)
        local color = math.floor((tonumber(noise.color) or 0) + 0.5)
        if luminance ~= 0 or color ~= 0 then
            parts[#parts + 1] = string.format("降噪 %d/%d", luminance, color)
        end
    end

    local channels = hsl_channel_count(parameters.hsl)
    if channels > 0 then
        parts[#parts + 1] = string.format("HSL %d 个通道", channels)
    end

    if color_grading_used(parameters.color_grading) then
        parts[#parts + 1] = "色彩分级"
    end

    if tone_curve_customised(parameters.tone_curve) then
        parts[#parts + 1] = "色调曲线"
    end

    local crop = parameters.crop_suggestion
    if type(crop) == "table" and crop.aspect_ratio then
        parts[#parts + 1] = "构图建议 " .. tostring(crop.aspect_ratio)
    end

    if #parts == 0 then
        return "本次没有需要应用的调整。"
    end

    return utf8_safe_truncate(table.concat(parts, " · "), 900, "...")
end

local function preview_style(catalog, photo, develop_settings, label)
    if type(develop_settings) ~= "table" then
        return nil, "服务端没有返回可用的 Lightroom 参数。"
    end
    -- Deliberately not wrapped in pcall/xpcall: the catalog write yields, and
    -- Lua 5.1 forbids yielding across a C call boundary (that was the 1.1.1
    -- "Yielding is not allowed within a C or metamethod call" crash).
    log("review: writing settings for " .. tostring(label))
    catalog:withWriteAccessDo("AI Photo Agent 预览修图", function()
        photo:applyDevelopSettings(
            develop_settings,
            "AI Photo Agent 预览 - " .. tostring(label),
            false
        )
    end, { timeout = 30 })
    return true
end

local function begin_preview_session(catalog, photo)
    local original = photo:getDevelopSettings()
    if type(original) ~= "table" then
        return nil, "无法读取这张照片当前的修图设置。"
    end
    catalog:withWriteAccessDo("AI Photo Agent 记录修改前状态", function()
        -- Only the first review of this photo may write the Before snapshot: a
        -- second review must not overwrite the state the recall path relies on
        -- (1.7.2). updateInPlace = true keeps exactly one snapshot across retries.
        local snapshots = photo:getDevelopSnapshots()
        local has_before = false
        if type(snapshots) == "table" then
            for _, snapshot in ipairs(snapshots) do
                if type(snapshot) == "table" and snapshot.name == PREVIEW_SNAPSHOT_NAME then
                    has_before = true
                    break
                end
            end
        end
        if not has_before then
            photo:createDevelopSnapshot(PREVIEW_SNAPSHOT_NAME, true)
        end
    end, { timeout = 30 })
    return original
end

local function delete_preview_snapshot(catalog, photo)
    local snapshots = photo:getDevelopSnapshots()
    if type(snapshots) ~= "table" then
        return
    end
    for _, snapshot in ipairs(snapshots) do
        if type(snapshot) == "table" and snapshot.name == PREVIEW_SNAPSHOT_NAME then
            local snapshot_id = snapshot.snapshotID or snapshot.id
            if snapshot_id then
                catalog:withWriteAccessDo("AI Photo Agent 清理 Before 快照", function()
                    photo:deleteDevelopSnapshot(snapshot_id)
                end, { timeout = 30 })
            end
        end
    end
end

local function apply_original(catalog, photo, original)
    if type(original) ~= "table" then
        return nil, "没有记录到修改前的修图设置。"
    end
    catalog:withWriteAccessDo("AI Photo Agent 还原修改前设置", function()
        photo:applyDevelopSettings(original, "AI Photo Agent 还原修改前设置", false)
    end, { timeout = 30 })
    return true
end

local function restore_preview(catalog, photo, original)
    apply_original(catalog, photo, original)
    delete_preview_snapshot(catalog, photo)
end

local function adaptive_note(development)
    if type(development) ~= "table" then
        return ""
    end
    local strength = tonumber(development.strength)
    if not strength or strength >= 0.995 then
        return ""
    end
    return string.format("（已按原图亮度自适应到 %d%%）", math.floor(strength * 100 + 0.5))
end

local function parameter_summary_of(development)
    if type(development) ~= "table" then
        return "服务端未返回参数明细。"
    end
    local summary = development.parameters_summary
    if type(summary) == "string" and summary ~= "" then
        return summary .. adaptive_note(development)
    end
    return format_parameters(development.parameters) .. adaptive_note(development)
end

local function confirm_keep(style_name, intensity, development)
    local percent = math.floor((tonumber(intensity) or 0) * 100 + 0.5)
    local info = parameter_summary_of(development)
    if development.preset_name and tostring(development.preset_name) ~= "" then
        info = info .. "\n\nXMP 预设：" .. tostring(development.preset_name)
    end
    info = info
        .. "\n\n现在照片上显示的就是这套参数的真实效果。"
        .. "按 \\ 键可在 Lightroom 中对比修改前后；"
        .. "点“取消并还原”会把照片恢复到修改前的状态。"
    return LrDialogs.confirm(
        string.format("是否保留「%s」（强度 %d%%）？", tostring(style_name), percent),
        info,
        "保留这些调整",
        "取消并还原",
        "换一个风格"
    )
end

--: 1.7.0: the review panel used to live only in Lua memory, so anything that
--: tore the review task down (Lightroom dropping a floating dialog, an exception
--: while closing the window) left the panel unreachable while the menu still had
--: no idea a review was in progress. The state below is written to disk at the
--: start of every review, so "显示分析窗口" can always build a fresh window.
local REVIEW_STATE_FILE = "review-state.json"
--: A saved review older than a week is much more likely to be an abandoned
--: session than something the user still wants to finish. Accepting it would
--: restore develop settings captured before later edits, so expire it.
local REVIEW_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
--: 1.7.2: the state used to be written inside the plug-in bundle, which breaks
--: on read-only installs, and disappears on upgrade. Prefer Lightroom's userData
--: directory; the old bundle path stays readable for backward compatibility.
local REVIEW_STATE_DIR_NAME = "AI Photo Agent"
--: Forward declaration: load_review_state() below deletes an expired file with it.
local clear_review_state


local function user_data_directory()
    local ok, base = pcall(LrPathUtils.getStandardFilePath, "userData")
    if ok and type(base) == "string" and base ~= "" then
        return base
    end
    return nil
end


local function review_state_directory()
    local base = user_data_directory()
    if not base then
        return nil
    end
    local directory = LrPathUtils.child(base, REVIEW_STATE_DIR_NAME)
    local created, create_error = pcall(LrFileUtils.createAllDirectories, directory)
    if not created then
        log("review: could not create the review state directory: "
            .. tostring(create_error))
        return nil
    end
    return directory
end


local function legacy_review_state_path()
    return LrPathUtils.child(_PLUGIN.path, REVIEW_STATE_FILE)
end


local function review_state_path()
    local directory = review_state_directory()
    if directory then
        return LrPathUtils.child(directory, REVIEW_STATE_FILE)
    end
    -- Hosts without a userData standard path keep the old bundle location.
    return legacy_review_state_path()
end


-- Write through a sibling temp file and move it into place: a crash halfway
-- through the old direct write left a truncated JSON file, which made the saved
-- review unrecoverable. LrFileUtils.writeFile does not exist on the tested
-- Lightroom build, so plain io remains the only writer.
local function write_text_file(path, contents)
    local temp_path = path .. ".tmp"
    local opened, file = pcall(io.open, temp_path, "w")
    if not opened or not file then
        return nil, tostring(file)
    end
    local wrote, write_error = pcall(function()
        file:write(contents)
        file:close()
    end)
    if not wrote then
        pcall(function()
            file:close()
        end)
        return nil, tostring(write_error)
    end
    if LrFileUtils.exists(path) == "file" then
        local removed, remove_error = pcall(LrFileUtils.delete, path)
        if not removed then
            log("review: could not replace the previous state file: "
                .. tostring(remove_error))
        end
    end
    local moved, move_error = pcall(LrFileUtils.move, temp_path, path)
    if not moved then
        log("review: could not move the review state into place: "
            .. tostring(move_error))
        return nil, tostring(move_error)
    end
    return true
end


local function read_text_file(path)
    -- LrFileUtils.readFile drops the last byte of every line (it mangled
    -- bridge-path.txt before), which would corrupt the saved JSON, so read with
    -- plain io instead.
    local opened, file = pcall(io.open, path, "r")
    if not opened or not file then
        return nil, tostring(file)
    end
    local read_ok, contents = pcall(function()
        return file:read("*a")
    end)
    pcall(function()
        file:close()
    end)
    if not read_ok then
        return nil, tostring(contents)
    end
    return contents
end


local function save_review_state(state)
    -- Json.encode does not yield, so pcall here is safe (unlike the catalog and
    -- HTTP calls, which must stay outside any C-boundary wrapper).
    state.saved_at = os.time()
    local ok, encoded = pcall(Json.encode, state)
    if not ok or type(encoded) ~= "string" then
        log("review: could not encode the review state: " .. tostring(encoded))
        return false
    end
    local path = review_state_path()
    local written, write_error = write_text_file(path, encoded)
    if written == nil then
        log("review: could not persist the review state to " .. tostring(path)
            .. ": " .. tostring(write_error))
        return false
    end
    log("review: saved the review state to " .. tostring(path)
        .. " (" .. tostring(#encoded) .. " bytes)")
    return true
end


local function decode_review_state(path)
    if LrFileUtils.exists(path) ~= "file" then
        return nil
    end
    local content, read_error = read_text_file(path)
    if type(content) ~= "string" or content == "" then
        log("review: the saved review state at " .. tostring(path)
            .. " is empty or unreadable: " .. tostring(read_error))
        return nil
    end
    local ok, decoded = pcall(Json.decode, content)
    if not ok or type(decoded) ~= "table" then
        log("review: could not decode the saved review state: " .. tostring(decoded))
        return nil
    end
    if type(decoded.analysis) ~= "table" or type(decoded.photo_id) ~= "string" then
        log("review: the saved review state is incomplete (analysis="
            .. type(decoded.analysis) .. ", photo_id=" .. type(decoded.photo_id) .. ")")
        return nil
    end
    local saved_at = tonumber(decoded.saved_at)
    if saved_at and (os.time() - saved_at) > REVIEW_STATE_TTL_SECONDS then
        log("review: the saved review state expired (saved_at="
            .. tostring(saved_at) .. ")")
        clear_review_state()
        return nil
    end
    return decoded
end


local function load_review_state()
    local path = review_state_path()
    local legacy_path = legacy_review_state_path()
    local candidates = { path }
    if legacy_path ~= path then
        candidates[#candidates + 1] = legacy_path
    end
    -- A crash between writing the temp file and moving it leaves the state one
    -- move away from recovery; read it as a last resort.
    candidates[#candidates + 1] = path .. ".tmp"
    for _, candidate in ipairs(candidates) do
        local state = decode_review_state(candidate)
        if state then
            if candidate ~= path then
                log("review: recovered the saved review state from "
                    .. tostring(candidate))
                save_review_state(state)
            end
            log("review: loaded the saved review state for photo "
                .. tostring(state.photo_id))
            return state
        end
    end
    log("review: no saved review state at " .. tostring(path))
    return nil
end


function clear_review_state()
    local paths = { review_state_path(), legacy_review_state_path() }
    paths[#paths + 1] = paths[1] .. ".tmp"
    local cleared = false
    for _, path in ipairs(paths) do
        if LrFileUtils.exists(path) == "file" then
            local removed, remove_error = pcall(LrFileUtils.delete, path)
            if removed then
                cleared = true
            else
                log("review: could not clear the saved review state: "
                    .. tostring(remove_error))
            end
        end
    end
    if cleared then
        log("review: cleared the saved review state")
    end
end


--: 1.7.1: Lightroom 15.1 floating dialogs are not always-on-top and a taskbar
--: thumbnail click cannot reliably raise a covered review window. Lightroom
--: 15.2 fixed that by forcing these dialogs topmost; the PowerShell helper
--: emulates it on 15.1 (Win32 only). It is best effort: a missing script, a
--: locked-down PowerShell or a non-Windows host only logs and leaves the 1.7.0
--: menu recall as the fallback.
local function start_review_window_topmost()
    if not Config.KEEP_REVIEW_WINDOW_ON_TOP then
        log("review: topmost helper disabled by Config.KEEP_REVIEW_WINDOW_ON_TOP")
        return
    end
    -- Lightroom's embedded Lua strips the `package` library (1.7.1 crashed
    -- here while detecting Windows). LrPathUtils is part of the SDK and is
    -- already used for the debug log paths; a POSIX temp path positively
    -- identifies macOS.
    local temp_dir = LrPathUtils.getStandardFilePath("temp")
    if type(temp_dir) == "string" and temp_dir:sub(1, 1) == "/" then
        log("review: topmost helper skipped (not Windows)")
        return
    end
    local script = LrPathUtils.child(_PLUGIN.path, "ReviewWindowTopmost.ps1")
    if LrFileUtils.exists(script) ~= "file" then
        log("review: topmost helper is missing at " .. tostring(script))
        return
    end
    local quoted_script, unsafe_reason = ps_quote(script)
    if not quoted_script then
        log("review: topmost helper path cannot be quoted safely: "
            .. tostring(unsafe_reason))
        return
    end
    local command = string.format(
        'powershell -NoProfile -WindowStyle Hidden -Command "Start-Process'
            .. " -FilePath 'powershell.exe' -ArgumentList"
            .. " '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',%s"
            .. ' -WindowStyle Hidden"',
        quoted_script
    )
    log("review: starting topmost helper: " .. command)
    -- LrTasks.execute yields; a standard pcall around it would abort with
    -- "Yielding is not allowed". The caller wraps this whole function in
    -- LrTasks.pcall, which is the SDK's yield-safe variant.
    local exit_code = LrTasks.execute(command)
    log("review: topmost helper launcher exited with " .. tostring(exit_code))
end


local function review_analysis(photo_id, analysis, catalog, photo, original)
    -- 1.4.0: one floating window replaces the message box + style picker +
    -- confirmation dialog. All I/O is injected here so ReviewWindow stays UI.
    -- 1.7.1: start the Windows topmost helper before the window exists; it
    -- finds the review frame by title and exits on its own when it disappears.
    -- 1.7.2: LrTasks.pcall is the yield-safe guard (standard pcall is forbidden
    -- around LrTasks.execute). An optional helper must never abort the review:
    -- a failure is logged and the floating window still opens.
    local topmost_ok, topmost_error = LrTasks.pcall(start_review_window_topmost)
    if not topmost_ok then
        log("review: topmost helper failed to start: " .. tostring(topmost_error))
    end
    local function fetch_development(style_id, intensity)
        local development, develop_error = Http.post_json(
            "/api/photos/" .. tostring(photo_id) .. "/develop",
            {
                style_id = style_id,
                intensity = intensity,
            }
        )
        if not development then
            return nil, develop_error
        end
        return development
    end

    local function apply_preview(development)
        return preview_style(
            catalog,
            photo,
            development.develop_settings,
            development.style_name
        )
    end

    local function apply_original_settings()
        return apply_original(catalog, photo, original)
    end

    local function on_keep(development, style_id, intensity)
        log(string.format(
            "analyze_and_apply: kept style %s at %d%%",
            tostring(development.style_name or style_id),
            math.floor((tonumber(intensity) or 0) * 100 + 0.5)
        ))
    end

    return ReviewWindow.run {
        analysis_text = format_analysis(analysis),
        styles = analysis.recommended_styles,
        log = log,
        fetch_development = fetch_development,
        apply_preview = apply_preview,
        apply_original = apply_original_settings,
        parameter_summary = parameter_summary_of,
        on_keep = on_keep,
    }
end

function Actions.resume_review(state)
    -- Rebuild the review window from the persisted state. Used when the live
    -- review task is gone but the user still wants the panel back.
    if not (Config.USE_FLOATING_REVIEW and ReviewWindow.available()) then
        log("resume_review: floating review is disabled or unavailable")
        return false
    end
    local catalog = LrApplication.activeCatalog()
    if not catalog then
        log("resume_review: no active catalog")
        return false
    end
    local photo = catalog:getTargetPhoto()
    if not photo then
        log("resume_review: no target photo")
        show_bezel("AI Photo Agent：请先选中照片，再点「显示分析窗口」", 4)
        return true
    end
    local path = photo:getRawMetadata("path")
    if type(state.photo_path) == "string" and state.photo_path ~= ""
        and tostring(path) ~= state.photo_path then
        log("resume_review: selected photo differs from the review photo")
        show_bezel("AI Photo Agent：请先选中上次分析的那张照片", 4)
        return true
    end
    if type(state.original) ~= "table" then
        log("resume_review: the saved original settings are missing")
        show_bezel("AI Photo Agent：上次的修改前状态没有保存下来，请重新分析一次", 5)
        return false
    end

    log("resume_review: rebuilding the review window from the saved state")
    local decision, window_error = review_analysis(
        state.photo_id,
        state.analysis,
        catalog,
        photo,
        state.original
    )
    if not decision then
        log("resume_review: the floating window could not be rebuilt: " .. tostring(window_error))
        if window_error == "closed" then
            -- The user dismissed the rebuilt window: same meaning as "cancel".
            restore_preview(catalog, photo, state.original)
            clear_review_state()
            log("resume_review: the review window was closed; restored the original settings")
            show_bezel("AI Photo Agent：已关闭修图窗口，照片已还原", 3)
            return true
        end
        -- No error dialog here: the caller falls back to the modal flow.
        return false
    end
    if decision == "keep" then
        clear_review_state()
        show_bezel("AI Photo Agent：已保留这套修图参数", 3.5)
        return true
    end
    restore_preview(catalog, photo, state.original)
    clear_review_state()
    log("resume_review: restored original settings")
    show_bezel("AI Photo Agent：已取消，照片已还原", 3)
    return true
end


function Actions.review_with_state(state)
    -- Guaranteed-reachable fallback. LrDialogs.presentModalDialog always sits on
    -- top, so even when the floating panel cannot exist this flow finishes the
    -- job: choose style -> preview -> keep / switch / revert.
    local catalog = LrApplication.activeCatalog()
    if not catalog then
        return false
    end
    local photo = catalog:getTargetPhoto()
    if not photo then
        show_bezel("AI Photo Agent：请先选中照片", 4)
        return true
    end
    if type(state.photo_path) == "string" and state.photo_path ~= ""
        and tostring(photo:getRawMetadata("path")) ~= state.photo_path then
        show_bezel("AI Photo Agent：请先选中上次分析的那张照片", 4)
        return true
    end
    local original = state.original
    if type(original) ~= "table" then
        return false
    end

    log("review_with_state: running the modal review flow")
    while true do
        local selection, selection_error = choose_style(state.analysis)
        if not selection then
            restore_preview(catalog, photo, original)
            clear_review_state()
            if selection_error then
                show_error(selection_error)
            else
                show_bezel("AI Photo Agent：已取消，照片已还原", 3)
            end
            return true
        end

        local development, develop_error = Http.post_json(
            "/api/photos/" .. tostring(state.photo_id) .. "/develop",
            {
                style_id = selection.style_id,
                intensity = selection.intensity,
            }
        )
        if not development then
            log("review_with_state: develop failed: " .. tostring(develop_error))
            restore_preview(catalog, photo, original)
            clear_review_state()
            show_error(develop_error)
            return true
        end

        local previewed, preview_error = preview_style(
            catalog,
            photo,
            development.develop_settings,
            development.style_name
        )
        if not previewed then
            log("review_with_state: preview failed: " .. tostring(preview_error))
            restore_preview(catalog, photo, original)
            clear_review_state()
            show_error(preview_error)
            return true
        end

        catalog:setSelectedPhotos(photo, {})
        LrApplicationView.switchToModule("develop")
        LrTasks.sleep(1.5)

        local verdict = confirm_keep(
            development.style_name,
            selection.intensity,
            development
        )
        log("review_with_state: verdict " .. tostring(verdict))
        if verdict == "ok" then
            clear_review_state()
            show_bezel("AI Photo Agent：已保留这套修图参数", 3.5)
            return true
        end
        if verdict ~= "other" then
            restore_preview(catalog, photo, original)
            clear_review_state()
            show_bezel("AI Photo Agent：已取消，照片已还原", 3)
            return true
        end
    end
end


function Actions.review_diagnostic()
    -- One line that answers "why is there no review window?" without asking the
    -- user to dig through the log: which review UI this build uses, whether a
    -- panel is live, and whether a saved state could be resumed.
    local loop_state = "已结束"
    if ReviewWindow.is_live() then
        loop_state = "运行中"
    elseif ReviewWindow.is_busy_alive() then
        loop_state = "预览中"
    elseif ReviewWindow.is_active() then
        loop_state = "未响应（可重建）"
    end
    local parts = {
        "评审界面=" .. (
            Config.USE_FLOATING_REVIEW and ReviewWindow.available()
                and "单窗口（浮动）"
                or "模态对话框"
        ),
        "窗口句柄=" .. (ReviewWindow.is_open() and "有" or "无"),
        "评审循环=" .. loop_state,
    }
    local path = review_state_path()
    if LrFileUtils.exists(path) ~= "file" then
        parts[#parts + 1] = "落盘状态=无"
    else
        local state = load_review_state()
        if state then
            parts[#parts + 1] = "落盘状态=可恢复 photo=" .. tostring(state.photo_id)
        else
            parts[#parts + 1] = "落盘状态=存在但无法解析"
        end
    end
    return table.concat(parts, " · ")
end


function Actions.show_review_window()
    -- Floating dialogs drop behind the Lightroom main window when the user
    -- clicks the catalog. This menu entry (and the dialog's selection observer)
    -- is how the panel comes back without re-running the analysis.
    log("show_review_window: invoked (floating=" .. tostring(Config.USE_FLOATING_REVIEW)
        .. ", open=" .. tostring(ReviewWindow.is_open())
        .. ", live=" .. tostring(ReviewWindow.is_live()) .. ")")
    -- 1.7.0: do not trust toFront() alone. On the tested Windows 15.1 build a
    -- floating dialog can be buried for good, and a loop that died silently
    -- leaves a stale handle. Ask the loop to close the old window and present a
    -- fresh one, then wait for its acknowledgement so a dead session falls
    -- through to the rebuild-from-disk path below instead of looking like a
    -- dead click.
    local recalled, recall_token = ReviewWindow.request_recall()
    if recalled then
        log("show_review_window: asked the review window to re-present itself")
        show_bezel("AI Photo Agent：正在重新打开修图窗口…", 3)
        -- Poll briefly instead of one long sleep: a live loop acknowledges in
        -- one 0.15 s tick, while a dead one never will and should fall through
        -- to the rebuild promptly. A busy preview is alive and is left alone.
        local waited = 0
        while waited < 2.0 do
            if ReviewWindow.recall_acknowledged(recall_token) then
                log("show_review_window: the review window re-presented itself")
                return
            end
            if ReviewWindow.is_busy_alive() then
                log("show_review_window: the review window is busy; it will re-present when the preview finishes")
                return
            end
            LrTasks.sleep(0.25)
            waited = waited + 0.25
        end
        log("show_review_window: the review loop did not answer; releasing the stale session")
        ReviewWindow.release_stale()
        recall_token = "stale"
    end
    log("show_review_window: no live review loop (" .. tostring(recall_token) .. ")")
    local state = load_review_state()
    if state then
        log("show_review_window: no live review session, rebuilding from the saved state")
        if Actions.resume_review(state) then
            return
        end
        log("show_review_window: falling back to the modal review flow")
        if Actions.review_with_state(state) then
            return
        end
    end
    -- Nothing to resume: never leave the click as a dead end. Offer to run a
    -- fresh analysis on whatever photo is selected instead.
    log("show_review_window: nothing to resume, offering a fresh analysis")
    local choice = LrDialogs.confirm(
        "AI Photo Agent",
        "没有找到未完成的评审。要现在分析当前选中的照片吗？",
        "开始分析",
        "取消"
    )
    if choice == "ok" then
        Actions.analyze_and_apply()
        return
    end
    show_bezel("AI Photo Agent：当前没有打开的分析窗口", 3)
end

function Actions.analyze_and_apply()
    -- Immediate feedback: the very first visible thing on click, before any
    -- Lightroom catalog work that could take a moment.
    log("analyze_and_apply: start")
    show_bezel("AI Photo Agent：正在准备照片…", 3)
    -- 1.7.0: starting a second review while one is live would fight over the
    -- same photo and the same review state. Send the user to the recall menu.
    if analysis_in_progress or ReviewWindow.is_live() or ReviewWindow.is_busy_alive() then
        log("analyze_and_apply: another review window or analysis is active; refusing to start a second one")
        show_bezel("AI Photo Agent：已经有一个修图窗口，点「显示分析窗口」把它调回来", 5)
        return
    end
    -- 1.7.2: hold the analysis lock until the whole flow (service bootstrap,
    -- upload, review) has finished. LrTasks.pcall is the yield-safe wrapper, so
    -- an exception can never leave the flag stuck and the next click blocked.
    analysis_in_progress = true
    local function run_analysis_flow()
    -- A previous session's saved state must not be cleared until this run owns
    -- the review flow; otherwise a failed analysis erases the recallable state.
    local previous_state = load_review_state()

    local photo, photo_error = get_target_photo()
    if not photo then
        log("analyze_and_apply: no target photo (" .. tostring(photo_error) .. ")")
        show_error(photo_error)
        return
    end

    local catalog = LrApplication.activeCatalog()
    if not catalog then
        log("analyze_and_apply: no active catalog")
        show_error("没有活动的 Lightroom 目录。")
        return
    end

    local service_ready, service_error = ensure_service()
    if not service_ready then
        log("analyze_and_apply: service unavailable: " .. tostring(service_error))
        show_error(service_error)
        return
    end

    local photo_id, analysis = upload_and_analyze(photo)
    if not photo_id then
        if analysis == ProgressWindow.CANCEL_MESSAGE then
            -- A deliberate cancel is not an error: no red dialog, no restore,
            -- because nothing was applied to the photo yet.
            log("analyze_and_apply: analysis cancelled by user")
            show_bezel("AI Photo Agent：已取消分析，照片没有改动", 3)
        else
            show_error(analysis)
        end
        return
    end

    local original, original_error = begin_preview_session(catalog, photo)
    if not original then
        log("analyze_and_apply: snapshot failed: " .. tostring(original_error))
        show_error(original_error)
        return
    end
    log("analyze_and_apply: before snapshot written")

    -- Persist everything the review window needs, so the panel can be rebuilt
    -- even if this task dies (see Actions.resume_review).
    local review_state = {
        photo_id = photo_id,
        photo_path = tostring(photo:getRawMetadata("path") or ""),
        analysis = analysis,
        original = original,
    }
    save_review_state(review_state)

    -- Put the photo on screen in the Develop module before opening the review
    -- window: the preview writes develop settings, so the change is only ever
    -- visible on that view.
    catalog:setSelectedPhotos(photo, {})
    LrApplicationView.switchToModule("develop")
    LrTasks.sleep(0.8)

    -- The single floating window keeps the Develop view visible and is the
    -- default review UI (1.7.0). The modal three-step flow is kept as the
    -- fallback for SDKs without presentFloatingDialog and for a window that
    -- cannot be built at all.
    if Config.USE_FLOATING_REVIEW and ReviewWindow.available() then
        log("analyze_and_apply: using the single-window review flow")
        local decision, window_error = review_analysis(
            photo_id,
            analysis,
            catalog,
            photo,
            original
        )
        if not decision then
            log("analyze_and_apply: review window failed: " .. tostring(window_error))
            if window_error == "已经有一个分析窗口打开了。" then
                -- Another review became live between the guard above and this
                -- call. Never start the modal flow on top of it; undo this
                -- run's snapshot and give the previous session its state back.
                restore_preview(catalog, photo, original)
                if previous_state then
                    save_review_state(previous_state)
                else
                    clear_review_state()
                end
                show_bezel("AI Photo Agent：已经有一个修图窗口，点「显示分析窗口」把它调回来", 5)
                return
            end
            if window_error == "closed" then
                -- The user closed the review window: treat it as a cancel.
                restore_preview(catalog, photo, original)
                clear_review_state()
                log("analyze_and_apply: the review window was closed; restored the original settings")
                show_bezel("AI Photo Agent：已关闭修图窗口，照片已还原", 3)
                return
            end
            log("analyze_and_apply: falling back to the modal review flow")
            -- Same analysis and same original settings, no second analysis run.
            if not Actions.review_with_state(review_state) then
                restore_preview(catalog, photo, original)
                clear_review_state()
                show_error(window_error)
            end
            return
        end
        if decision == "keep" then
            clear_review_state()
            show_bezel("AI Photo Agent：已保留这套修图参数", 3.5)
            return
        end
        restore_preview(catalog, photo, original)
        clear_review_state()
        log("analyze_and_apply: restored original settings")
        show_bezel("AI Photo Agent：已取消，照片已还原", 3)
        return
    end

    -- SDK < 5.0 (or a deliberately disabled floating window): modal dialogs
    -- always sit on top, so this three-step flow can always be completed.
    log("analyze_and_apply: using the modal review flow")
    LrDialogs.message("AI Photo Agent - 修图建议", format_analysis(analysis), "info")

    while true do
        local selection, selection_error = choose_style(analysis)
        if not selection then
            log("analyze_and_apply: style picker cancelled: " .. tostring(selection_error))
            restore_preview(catalog, photo, original)
            clear_review_state()
            if selection_error then
                -- Not a user cancel: the analysis carried no usable style.
                show_error(selection_error)
            else
                show_bezel("AI Photo Agent：已取消，照片已还原", 3)
            end
            return
        end
        log("analyze_and_apply: previewing style " .. tostring(selection.style_id))

        local development, develop_error = Http.post_json(
            "/api/photos/" .. tostring(photo_id) .. "/develop",
            {
                style_id = selection.style_id,
                intensity = selection.intensity,
            }
        )
        if not development then
            log("analyze_and_apply: develop failed: " .. tostring(develop_error))
            restore_preview(catalog, photo, original)
            clear_review_state()
            show_error(develop_error)
            return
        end

        local previewed, preview_error = preview_style(
            catalog,
            photo,
            development.develop_settings,
            development.style_name
        )
        if not previewed then
            log("analyze_and_apply: preview failed: " .. tostring(preview_error))
            restore_preview(catalog, photo, original)
            clear_review_state()
            show_error(preview_error)
            return
        end

        catalog:setSelectedPhotos(photo, {})
        LrApplicationView.switchToModule("develop")
        show_bezel(
            string.format(
                "已把「%s」试用到这张照片上，按 \\ 对比修改前后",
                tostring(development.style_name)
            ),
            5
        )
        log("analyze_and_apply: preview applied, waiting for confirmation")

        -- Give Lightroom a moment to repaint the Develop module behind the
        -- bezel: switching module and opening a modal dialog back to back can
        -- leave the photo half-rendered under the dialog.
        LrTasks.sleep(1.5)

        local verdict = confirm_keep(
            development.style_name,
            selection.intensity,
            development
        )
        log("analyze_and_apply: verdict " .. tostring(verdict))

        if verdict == "ok" then
            clear_review_state()
            show_bezel(
                string.format(
                    "AI Photo Agent：已保留「%s」的调整",
                    tostring(development.style_name)
                ),
                3.5
            )
            return
        end

        if verdict ~= "other" then
            restore_preview(catalog, photo, original)
            clear_review_state()
            log("analyze_and_apply: restored original settings")
            show_bezel("AI Photo Agent：已取消，照片已还原", 3)
            return
        end

        log("analyze_and_apply: switching preview to another style")
    end
    end
    local run_ok, run_error = LrTasks.pcall(run_analysis_flow)
    analysis_in_progress = false
    if not run_ok then
        log("analyze_and_apply: aborted: " .. tostring(run_error))
        error(run_error)
    end
end

local function bridge_candidates()
    local candidates = {}
    local function add(path)
        if path and path ~= "" then
            candidates[#candidates + 1] = path
        end
    end

    local plugin_root = LrPathUtils.parent(LrPathUtils.parent(_PLUGIN.path))
    add(plugin_root)

    local app_prefs = LrPathUtils.getStandardFilePath("appPrefs")
    add(app_prefs)
    if app_prefs then
        add(LrPathUtils.parent(app_prefs))
    end

    local app_data = LrPathUtils.getStandardFilePath("appData")
    add(app_data)
    if app_data then
        add(LrPathUtils.child(LrPathUtils.child(app_data, "Adobe"), "Lightroom"))
    end

    local home = LrPathUtils.getStandardFilePath("home")
    if home then
        add(LrPathUtils.child(
            LrPathUtils.child(
                LrPathUtils.child(home, "AppData"),
                "Roaming"
            ),
            "Adobe/Lightroom"
        ))
        add(LrPathUtils.child(
            LrPathUtils.child(
                LrPathUtils.child(
                    LrPathUtils.child(home, "Library"),
                    "Application Support"
                ),
                "Adobe"
            ),
            "Lightroom"
        ))
    end
    return candidates
end

-- 1.7.2: the watcher probes for the bridge every 3 seconds; the hint file and
-- the candidate directories do not change that often. Cache a found directory
-- briefly so polling stops re-reading and re-statting the same paths.
local BRIDGE_DIR_CACHE_SECONDS = 60
local bridge_dir_cache = nil
local bridge_dir_checked_at = 0
--: Last bridge failure, surfaced by the status dialog so a failed auto-apply is
--: visible instead of only logged.
local last_bridge_error = nil


local function find_bridge_dir(force_refresh)
    local now = os.time()
    if not force_refresh and bridge_dir_cache
        and (now - bridge_dir_checked_at) < BRIDGE_DIR_CACHE_SECONDS
        and LrFileUtils.exists(bridge_dir_cache) == "directory" then
        return bridge_dir_cache
    end
    -- Use the plug-in's io reader: LrFileUtils.readFile drops the last byte of
    -- every line and once mangled this very hint file.
    local hint_path = LrPathUtils.child(_PLUGIN.path, "bridge-path.txt")
    local hint = read_text_file(hint_path)
    if type(hint) == "string" then
        hint = hint:match("^%s*(.-)%s*$")
        if hint and hint ~= "" and LrFileUtils.exists(hint) == "directory" then
            debug_log("bridge hint: " .. hint)
            bridge_dir_cache = hint
            bridge_dir_checked_at = now
            return hint
        end
    end

    local candidates = bridge_candidates()
    for _, root in ipairs(candidates) do
        local bridge = LrPathUtils.child(root, Config.BRIDGE_DIR_NAME)
        local state = LrFileUtils.exists(bridge)
        debug_log("candidate " .. tostring(bridge) .. " => " .. tostring(state))
        if state == "directory" then
            debug_log("bridge dir: " .. tostring(bridge))
            bridge_dir_cache = bridge
            bridge_dir_checked_at = now
            return bridge
        end
    end
    if candidates[1] then
        local fallback = LrPathUtils.child(candidates[1], Config.BRIDGE_DIR_NAME)
        debug_log("bridge fallback: " .. tostring(fallback))
        return fallback
    end
    debug_log("bridge dir not found")
    return nil
end

local function list_jobs(inbox)
    local jobs = {}
    if LrFileUtils.exists(inbox) ~= "directory" then
        return jobs
    end
    for path in LrFileUtils.files(inbox) do
        if path:lower():match("%.json$") then
            jobs[#jobs + 1] = path
        end
    end
    table.sort(jobs)
    return jobs
end

local function safe_move(path, destination_directory)
    LrFileUtils.createAllDirectories(destination_directory)
    local file_name = LrPathUtils.leafName(path)
    local destination = LrPathUtils.child(destination_directory, file_name)
    local ok, message = LrFileUtils.move(path, destination)
    if not ok then
        log("Unable to move " .. tostring(path) .. ": " .. tostring(message))
    end
    return ok
end

local function read_job(path)
    -- Same io reader as the review state: LrFileUtils.readFile drops the last
    -- byte of each line, which can corrupt the job JSON written by the service.
    local content, read_error = read_text_file(path)
    if type(content) ~= "string" or content == "" then
        return nil, "无法读取待处理任务文件：" .. tostring(read_error)
    end
    local ok, job = pcall(Json.decode, content)
    if not ok or type(job) ~= "table" then
        return nil, "待处理任务 JSON 无效。"
    end
    if type(job.develop_settings) ~= "table" then
        return nil, "待处理任务缺少 develop_settings。"
    end
    return job
end

local function claim_job(bridge, path)
    local processing = LrPathUtils.child(bridge, "processing")
    LrFileUtils.createAllDirectories(processing)
    local destination = LrPathUtils.child(processing, LrPathUtils.leafName(path))
    local ok, move_error = LrFileUtils.move(path, destination)
    debug_log("claim job " .. tostring(path) .. " -> " .. tostring(ok) .. " " .. tostring(move_error))
    if not ok then
        return nil
    end
    return destination
end

--: 1.7.2 (contract update): the service sends job.photo.original_filename (the
--: uploaded file's basename) so the plug-in can prove the selected Lightroom
--: photo is the intended target. Older jobs may carry local_path or
--: local_file_name instead; if none of them is present, apply anyway but tell
--: the user the target could not be verified (documented residual risk).
local function job_local_identity(job)
    if type(job) ~= "table" or type(job.photo) ~= "table" then
        return nil
    end
    for _, key in ipairs({ "local_path", "local_file_name" }) do
        local value = job.photo[key]
        if type(value) == "string" and value ~= "" then
            return value
        end
    end
    return nil
end


local function job_original_filename(job)
    if type(job) ~= "table" or type(job.photo) ~= "table" then
        return nil
    end
    local value = job.photo.original_filename
    if type(value) == "string" then
        value = value:match("^%s*(.-)%s*$")
        if value ~= "" then
            return value
        end
    end
    return nil
end


local function job_has_identity(job)
    return job_original_filename(job) ~= nil or job_local_identity(job) ~= nil
end


local function photo_local_name(photo)
    -- photo:getRawMetadata("fileName") is the authoritative basename; fall back
    -- to the path leaf so an unusual metadata table still yields a comparison.
    local ok, file_name = pcall(photo.getRawMetadata, photo, "fileName")
    if ok and type(file_name) == "string" and file_name ~= "" then
        return file_name
    end
    local path = photo:getRawMetadata("path")
    if type(path) == "string" and path ~= "" then
        return LrPathUtils.leafName(path)
    end
    return nil
end


local function target_identity_mismatch(photo, job)
    -- Prefer the filename contract; keep tolerating the older path fields.
    local expected = job_original_filename(job) or job_local_identity(job)
    if not expected then
        return nil, false
    end
    local actual = photo_local_name(photo)
    if type(actual) ~= "string" or actual == "" then
        return "无法读取当前选中照片的文件名，已取消应用。请重新在 Web 端发送到 Lightroom。", true
    end
    local expected_name = LrPathUtils.leafName(expected)
    -- Windows filenames are case-insensitive and the service may include
    -- surrounding whitespace; compare trimmed, case-insensitive basenames.
    if string.lower(tostring(expected_name)) ~= string.lower(tostring(actual)) then
        return string.format(
            "任务目标照片与当前选中照片不一致：任务「%s」，当前「%s」。已取消应用，请在 Lightroom 中选中目标任务照片后重试。",
            tostring(expected_name),
            tostring(actual)
        ), true
    end
    return nil, true
end


local function process_job_file(path, known_photo)
    local job, job_error = read_job(path)
    if not job then
        return nil, job_error
    end

    local photo = known_photo
    if not photo then
        local photo_error
        photo, photo_error = get_target_photo()
        if not photo then
            return nil, photo_error
        end
    end

    local identity_error = target_identity_mismatch(photo, job)
    if identity_error then
        log("bridge: " .. identity_error)
        return nil, identity_error
    end

    local style_name = "AI Photo Agent"
    if type(job.style) == "table" and job.style.name then
        style_name = tostring(job.style.name)
    elseif job.preset_name then
        style_name = tostring(job.preset_name)
    end

    local _, apply_error = apply_to_virtual_copy(
        photo,
        style_name,
        job.develop_settings
    )
    if apply_error then
        return nil, apply_error
    end
    -- false means the service did not send a local identity: the caller asks the
    -- user to confirm the target photo instead of pretending it was verified.
    job.target_verified = job_has_identity(job)
    return job
end

local function finish_job(bridge, claimed_path, job, error_message)
    if job then
        last_bridge_error = nil
        safe_move(claimed_path, LrPathUtils.child(bridge, "done"))
        debug_log("Bridge job applied: " .. tostring(job.job_id or job.preset_name))
        log("Bridge job applied: " .. tostring(job.job_id or job.preset_name))
    else
        last_bridge_error = tostring(error_message)
        safe_move(claimed_path, LrPathUtils.child(bridge, "failed"))
        debug_log("Bridge job failed: " .. tostring(error_message))
        log("Bridge job failed: " .. tostring(error_message))
    end
end

--: 1.7.2: a crash between claim_job() and finish_job() used to strand a job
--: forever in processing/: it was neither retried nor reported. Requeue stale
--: claims into inbox/ (with an attempt counter) and move hopeless ones to
--: failed/; prune old done/failed artifacts so the bridge does not grow forever.
local PROCESSING_STALE_SECONDS = 10 * 60
local PROCESSING_MAX_ATTEMPTS = 3
local BRIDGE_PRUNE_SECONDS = 7 * 24 * 60 * 60


local function job_is_older_than(job_id, seconds)
    local stamp = type(job_id) == "string" and job_id:match("^job_(%d%d%d%d%d%d%d%d%d%d%d%d%d%d)_")
    if not stamp then
        return false
    end
    -- Job ids carry a UTC timestamp; compare lexically against the same format.
    local cutoff = os.date("!%Y%m%d%H%M%S", os.time() - seconds)
    return stamp <= cutoff
end


function Actions.recover_bridge_jobs()
    local bridge = find_bridge_dir()
    if not bridge then
        return
    end
    local processing = LrPathUtils.child(bridge, "processing")
    local inbox = LrPathUtils.child(bridge, "inbox")
    local failed = LrPathUtils.child(bridge, "failed")
    local requeued = 0
    local failed_moves = 0
    if LrFileUtils.exists(processing) == "directory" then
        for path in LrFileUtils.files(processing) do
            if path:lower():match("%.json$") then
                local job = read_job(path)
                local job_id = job and job.job_id
                    or LrPathUtils.leafName(path):gsub("%.json$", "")
                if job_is_older_than(job_id, PROCESSING_STALE_SECONDS) then
                    local attempts = tonumber(job and job.attempts) or 0
                    if job and attempts < PROCESSING_MAX_ATTEMPTS then
                        job.attempts = attempts + 1
                        local encoded_ok, encoded = pcall(Json.encode, job)
                        local target = LrPathUtils.child(inbox, LrPathUtils.leafName(path))
                        local written = encoded_ok and type(encoded) == "string"
                            and write_text_file(target, encoded)
                        if written then
                            pcall(LrFileUtils.delete, path)
                            requeued = requeued + 1
                        end
                    else
                        safe_move(path, failed)
                        failed_moves = failed_moves + 1
                    end
                end
            end
        end
    end
    local pruned = 0
    for _, directory in ipairs({ LrPathUtils.child(bridge, "done"), failed }) do
        if LrFileUtils.exists(directory) == "directory" then
            for path in LrFileUtils.files(directory) do
                if path:lower():match("%.json$") then
                    local job_id = LrPathUtils.leafName(path):gsub("%.json$", "")
                    if job_is_older_than(job_id, BRIDGE_PRUNE_SECONDS) then
                        local removed = pcall(LrFileUtils.delete, path)
                        if removed then
                            pruned = pruned + 1
                        end
                    end
                end
            end
        end
    end
    if requeued > 0 or failed_moves > 0 or pruned > 0 then
        log(string.format(
            "bridge: recovered %d stale job(s), failed %d, pruned %d old artifact(s)",
            requeued, failed_moves, pruned
        ))
    end
end


function Actions.apply_pending()
    local bridge = find_bridge_dir()
    if not bridge then
        show_error("没有找到 Lightroom 桥接目录。请先在 Web 端点击“发送到 Lightroom”。")
        return
    end
    local jobs = list_jobs(LrPathUtils.child(bridge, "inbox"))
    if #jobs == 0 then
        LrDialogs.message(
            "AI Photo Agent",
            "没有待处理任务。请先在 Web 端生成参数并点击“发送到 Lightroom”。",
            "info"
        )
        return
    end

    local photo, photo_error = get_target_photo()
    if not photo then
        show_error(photo_error)
        return
    end

    local claimed = claim_job(bridge, jobs[1])
    if not claimed then
        show_error("无法锁定待处理任务，请稍后重试。")
        return
    end
    local job, error_message = process_job_file(claimed, photo)
    finish_job(bridge, claimed, job, error_message)
    if not job then
        show_error(error_message)
        return
    end
    local note = ""
    if job.target_verified == false then
        note = "\n\n注意：服务端任务没有携带原始照片身份信息，无法自动校验目标照片，请核对当前选中的照片。"
        show_bezel("AI Photo Agent：已应用待处理修图（无法校验目标照片）", 4)
    end
    LrDialogs.message(
        "AI Photo Agent",
        "已应用待处理修图：" .. tostring(job.preset_name or job.job_id or "") .. note,
        "info"
    )
end

function Actions.poll_inbox()
    debug_log("poll start")
    local bridge = find_bridge_dir()
    if not bridge then
        return
    end
    local inbox = LrPathUtils.child(bridge, "inbox")
    local jobs = list_jobs(inbox)
    debug_log("poll jobs: " .. tostring(#jobs))
    if #jobs == 0 then
        return
    end

    -- Wait until a photo is selected. The job stays in the inbox.
    local catalog = LrApplication.activeCatalog()
    if not catalog then
        debug_log("poll: no active catalog")
        return
    end
    local photo = catalog:getTargetPhoto()
    if not photo then
        debug_log("poll: no target photo")
        return
    end
    if photo:getRawMetadata("isVideo") then
        debug_log("poll: target is video")
        return
    end

    local claimed = claim_job(bridge, jobs[1])
    if not claimed then
        return
    end
    local job, error_message = process_job_file(claimed, photo)
    finish_job(bridge, claimed, job, error_message)
    if not job then
        log("poll: bridge job failed: " .. tostring(error_message))
        show_bezel("AI Photo Agent：待处理修图失败，详情见「显示状态」", 5)
    elseif job.target_verified == false then
        log("poll: applied a job without a local target identity; the user must confirm the photo")
        show_bezel("AI Photo Agent：已应用待处理修图（无法校验目标照片，请核对）", 4)
    end
end

-- Probe used by the startup self-check so a single log line tells us whether
-- the plug-in's HTTP layer can reach the local service at all.
function Actions.availability_summary()
    local status, status_error = Http.get("/api/lightroom/status")
    if not status then
        return "unreachable (" .. tostring(status_error) .. ")"
    end
    return string.format(
        "ok detected=%s plugin_installed=%s",
        tostring(status.detected),
        tostring(status.plugin_installed)
    )
end

function Actions.show_status()
    local status, status_error = Http.get("/api/lightroom/status")
    if not status then
        log("status: service unreachable: " .. tostring(status_error))
        show_error(
            "无法连接本地 AI Photo Agent 服务。\n"
                .. tostring(status_error)
                .. "\n\n请确认已运行 run_app.py，且 Config.lua 中的 SERVER_URL 与服务地址一致。"
        )
        return
    end

    -- Info.lua exposes the custom key as uppercase VERSION; the lowercase
    -- _PLUGIN.version was never set, so the dialog always showed 1.1.0 fallback.
    local version = _PLUGIN.VERSION or _PLUGIN.version or {}
    local bridge = find_bridge_dir()
    local bridge_failed_count = 0
    if bridge then
        bridge_failed_count = #list_jobs(LrPathUtils.child(bridge, "failed"))
    end
    local lines = {
        "服务地址：" .. tostring(status.server_url or Config.SERVER_URL),
        "Lightroom 已检测：" .. (status.detected and "是" or "否"),
        "Lightroom 目录：" .. tostring(status.lightroom_root or "未检测到"),
        "插件已安装：" .. (status.plugin_installed and "是" or "否"),
        "预设目录：" .. tostring(status.presets_dir or "未检测到"),
        "桥接收件箱：" .. tostring(status.bridge_inbox or "未检测到"),
        "桥接失败任务："
            .. tostring(bridge_failed_count)
            .. (last_bridge_error and ("（最近：" .. tostring(last_bridge_error) .. "）") or ""),
        "分析进度窗口："
            .. (type(LrDialogs.showModalProgressDialog) == "function" and "可用" or "退化为提示条"),
        "自动启动服务："
            .. (Config.AUTO_START_SERVICE
                    and string.format(
                        "已启用（最多等待 %d 秒）",
                        tonumber(Config.SERVICE_START_WAIT_SECONDS) or 45
                    )
                or "已关闭"),
        "",
        tostring(status.message or ""),
        "",
        "插件版本：" .. string.format(
            "%d.%d.%d",
            tonumber(version.major) or 1,
            tonumber(version.minor) or 1,
            tonumber(version.revision) or 0
        ),
        "插件路径：" .. tostring(_PLUGIN.path),
        "评审状态：" .. Actions.review_diagnostic(),
    }
    log("status: shown (service ok)")
    LrDialogs.message("AI Photo Agent - 连接状态", table.concat(lines, "\n"), "info")
end

function Actions.open_web()
    local LrHttp = import "LrHttp"
    LrHttp.openUrlInBrowser(Config.SERVER_URL)
end

return Actions
