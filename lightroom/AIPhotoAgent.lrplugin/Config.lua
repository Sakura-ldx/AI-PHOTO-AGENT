-- Local AI Photo Agent service settings.
-- Keep machine-specific values in Config.local.lua (git-ignored), not here.
local lrPathUtils = type(import) == "function" and import("LrPathUtils") or nil
local Config = {
    SERVER_URL = "http://127.0.0.1:8000",
    REQUEST_TIMEOUT = 600,
    UPLOAD_TIMEOUT = 180,
    STATUS_TIMEOUT = 15,
    THUMBNAIL_SIZE = 2048,
    -- Some photos cannot produce a JPEG thumbnail at full size (16-bit TIFFs,
    -- damaged preview cache). Retry once at this size before falling back.
    THUMBNAIL_RETRY_SIZE = 1024,
    -- Fallback upload: when Lightroom refuses to render a thumbnail we send the
    -- original file instead. Keep this under the service's upload limit.
    MAX_UPLOAD_BYTES = 200 * 1024 * 1024,
    POLL_SECONDS = 3,
    -- Analysis job polling: one lightweight GET per second while the service
    -- works, so Lightroom can show a live progress panel.
    JOB_POLL_SECONDS = 1,
    JOB_TIMEOUT_SECONDS = 600,
    PROGRESS_MIN_VISIBLE = 1.2,
    BRIDGE_DIR_NAME = "AI Photo Agent Bridge",
    -- Analysis provider name understood by the service ("default" picks the
    -- provider configured in the app settings).
    DEFAULT_PROVIDER = "default",
    -- When a menu action cannot reach SERVER_URL, launch the local service
    -- instead of only reporting an error. Windows only: the launch command
    -- uses PowerShell's Start-Process so the window stays hidden.
    AUTO_START_SERVICE = true,
    SERVICE_START_WAIT_SECONDS = 45,
    -- Optional absolute project path, used when the plug-in is loaded from the
    -- installed Modules copy and cannot derive the project root from
    -- _PLUGIN.path. Leave empty for auto-detection, or set it in
    -- Config.local.lua on your machine.
    SERVICE_PROJECT_DIR = "",
    DEBUG = false,
    -- Keep this false until createVirtualCopy is certified on the installed
    -- Lightroom build. The original photo is protected by a Before snapshot.
    USE_VIRTUAL_COPY = false,
    -- Review UI: the single floating review window is the default again
    -- (1.7.0). It is the only presentation that keeps the photo visible while
    -- the preview is written to it, so the user can adjust style/intensity and
    -- see the real result in one place. The menu entry "显示分析窗口" now asks
    -- the running loop to re-present a fresh window, or rebuilds it from
    -- review-state.json when the loop died. Set this to false to force the
    -- modal three-step flow (SDK < 5.0 fallback / experiments).
    USE_FLOATING_REVIEW = true,
    -- Windows only (1.7.1): Lightroom 15.1 floating dialogs are not always on
    -- top, so the main window can cover the review window and a taskbar
    -- thumbnail click cannot raise it again. Lightroom 15.2 fixed this by
    -- forcing such dialogs topmost; ReviewWindowTopmost.ps1 emulates that on
    -- 15.1 (topmost while Lightroom is foreground, no minimize box, no focus
    -- stealing). Set false to disable the helper; the menu recall still works.
    KEEP_REVIEW_WINDOW_ON_TOP = true,
}

-- Optional machine-local overrides. Copy Config.local.example.lua to
-- Config.local.lua and adjust it; the real file is git-ignored.
if type(_PLUGIN) == "table" and lrPathUtils then
    local ok, localConfig = pcall(function()
        return dofile(lrPathUtils.child(_PLUGIN.path, "Config.local.lua"))
    end)
    if ok and type(localConfig) == "table" then
        for key, value in pairs(localConfig) do
            Config[key] = value
        end
    end
end

return Config
