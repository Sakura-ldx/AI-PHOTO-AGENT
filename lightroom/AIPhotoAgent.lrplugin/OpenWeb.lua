-- Menu entry: open the local web UI in the default browser.
--
-- See AnalyzeSelected.lua for why the action is not wrapped in pcall and why an
-- error dialog is attached to the context.

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentOpenWeb", function(context)
    LrDialogs.attachErrorDialogToFunctionContext(context)
    context:addCleanupHandler(function()
        Actions.log_info("menu: open web finished")
    end)

    Actions.log_info("menu: open web invoked")
    Actions.open_web()
end)
