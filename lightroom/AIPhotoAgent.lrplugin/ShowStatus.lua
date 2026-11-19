-- Menu entry: show the local service / plug-in / bridge status.
--
-- See AnalyzeSelected.lua for why the action is not wrapped in pcall and why an
-- error dialog is attached to the context.

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentShowStatus", function(context)
    LrDialogs.attachErrorDialogToFunctionContext(context)
    context:addCleanupHandler(function()
        Actions.log_info("menu: show status finished")
    end)

    Actions.log_info("menu: show status invoked")
    Actions.show_status()
end)
