-- Menu entry: bring the analysis/review window back to the front.
--
-- Lightroom pushes a floating dialog behind the main window as soon as the user
-- clicks the catalog, and the SDK has no "always on top" flag. Clicking the
-- filmstrip or changing the active source already re-raises it (the dialog's
-- selectionChangeObserver/sourceChangeObserver call ReviewWindow.bring_to_front),
-- but a user who clicks empty chrome can still lose the panel. This menu item is
-- the escape hatch that always works.
--
-- Do NOT wrap the action in pcall: Lightroom yielding APIs cannot resume across
-- the C-call boundary that pcall introduces (Lua 5.1).

local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"

local Actions = dofile(LrPathUtils.child(_PLUGIN.path, "Actions.lua"))

LrFunctionContext.postAsyncTaskWithContext("AIPhotoAgentShowReview", function(context)
    LrDialogs.attachErrorDialogToFunctionContext(context)
    context:addCleanupHandler(function()
        Actions.log_info("menu: show-review finished")
    end)

    Actions.log_info("menu: show-review invoked")
    Actions.show_review_window()
end)
