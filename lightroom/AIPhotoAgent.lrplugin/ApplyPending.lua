-- Menu entry: apply the oldest pending bridge job from the web UI.
--
-- See AnalyzeSelected.lua for why the action is not wrapped in pcall and why an
-- error dialog is attached to the context.

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentApplyPending", function(context)
    LrDialogs.attachErrorDialogToFunctionContext(context)
    context:addCleanupHandler(function()
        Actions.log_info("menu: apply pending finished")
    end)

    Actions.log_info("menu: apply pending invoked")
    Actions.apply_pending()
end)
