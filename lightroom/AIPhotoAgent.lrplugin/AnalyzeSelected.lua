-- Menu entry: analyze the photo that is selected in Lightroom.
--
-- Lightroom runs menu entries inside an async task. An error raised there is
-- swallowed unless a dialog is attached to the context, which is why the menu
-- item used to "do nothing" without a single log line. So every entry attaches
-- an error dialog, logs that the click arrived, and logs a footer when it ends.
--
-- Do NOT wrap the action in pcall. Lightroom's yielding APIs (LrTasks.sleep,
-- LrHttp.*, LrDialogs.showModalProgressDialog ...) cannot resume across a
-- C-call boundary in Lua 5.1, and pcall *is* such a boundary: the call aborts
-- with "Yielding is not allowed within a C or metamethod call". Error reporting
-- comes from attachErrorDialogToFunctionContext instead.

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentAnalyze", function(context)
    LrDialogs.attachErrorDialogToFunctionContext(context)
    context:addCleanupHandler(function()
        Actions.log_info("menu: analyze finished")
    end)

    Actions.log_info("menu: analyze invoked")
    Actions.analyze_and_apply()
end)
