-- Watch the local bridge inbox so a "send to Lightroom" action in the web UI
-- is applied as soon as a photo is selected in Lightroom.

local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"
local LrTasks = import "LrTasks"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

Actions.debug_log("InitPlugin loaded: " .. tostring(_PLUGIN.path))
Actions.log_info("InitPlugin loaded: " .. tostring(_PLUGIN.path))

if not _G._AIPhotoAgentWatcherStarted then
    _G._AIPhotoAgentWatcherStarted = true
    LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentWatcher", function(context)
        context:addCleanupHandler(function()
            _G._AIPhotoAgentWatcherStarted = false
        end)
        Actions.debug_log("watcher task started")
        Actions.log_info("watcher task started")
        -- One-shot self-check: a single line that proves whether the plug-in's
        -- Lua HTTP layer can reach the local service. It runs inside the async
        -- task so a stopped service never delays Lightroom's startup.
        --
        -- No pcall here either: Http.get yields, and a yield may not cross the
        -- C-call boundary of pcall (Lua 5.1). Http.get reports a dead service
        -- as nil + message instead of raising, which is what this needs.
        local summary = Actions.availability_summary()
        Actions.log_info(
            "self-check: plugin="
                .. tostring(_PLUGIN.path)
                .. " server="
                .. tostring(Actions.Config.SERVER_URL)
                .. " service="
                .. tostring(summary)
        )
        -- 1.7.2: recover jobs a crash left in processing/ and prune old
        -- done/failed artifacts once per Lightroom session.
        LrTasks.pcall(Actions.recover_bridge_jobs)
        while true do
            -- LrTasks.pcall is the SDK's yield-safe guard (a standard pcall
            -- cannot wrap yielding Lightroom APIs). One bad catalog write or
            -- bridge job used to kill this watcher forever, leaving auto-apply
            -- silently dead until Lightroom was restarted.
            local poll_ok, poll_error = LrTasks.pcall(Actions.poll_inbox)
            if not poll_ok then
                Actions.log_info("watcher: poll failed: " .. tostring(poll_error))
                Actions.debug_log("watcher: poll failed: " .. tostring(poll_error))
            end
            LrTasks.sleep(Actions.Config.POLL_SECONDS)
        end
    end)
end
