--[[
AI Photo Agent - Lightroom Classic plug-in metadata.

The plug-in talks to the local AI Photo Agent HTTP service, uploads a rendered
JPEG preview of the selected photo, shows the analysis and suggested styles,
previews the generated develop settings on the selected photo, and only keeps
them after the user confirms.

Menu placement: the SDK only defines three menu sections. `LrLibraryMenuItems`
lands in 图库 > 增效工具附加功能 and is only visible while the Library module is
active. `LrExportMenuItems` lands in 文件 > 增效工具附加功能 and is visible in
every module, including 修改照片. Both point at the same four actions so the
Develop workflow never has to switch back to the Library module.
]]

local function menu_items()
    return {
        {
            title = "AI Photo Agent - 分析并修图（当前照片）",
            file = "AnalyzeSelected.lua",
        },
        {
            title = "AI Photo Agent - 应用 Web 端待处理修图",
            file = "ApplyPending.lua",
        },
        {
            title = "AI Photo Agent - 连接与安装状态",
            file = "ShowStatus.lua",
        },
        {
            title = "AI Photo Agent - 打开 Web 修图页面",
            file = "OpenWeb.lua",
        },
        {
            title = "AI Photo Agent - 显示分析窗口",
            file = "ShowReview.lua",
        },
    }
end

return {
    LrSdkVersion = 10.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = "com.aiphotoagent.lightroom",
    LrPluginName = "AI Photo Agent",
    LrInitPlugin = "InitPlugin.lua",

    -- 图库 > 增效工具附加功能（只在图库模块可见）
    LrLibraryMenuItems = menu_items(),
    -- 文件 > 增效工具附加功能（任何模块可见，修改照片模块里也能直接点）
    LrExportMenuItems = menu_items(),

    VERSION = { major = 1, minor = 7, revision = 2 },
}
