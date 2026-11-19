# AI Photo Agent Lightroom Classic Plug-in

Install this folder with `File > Plug-in Manager... > Add` in Lightroom Classic,
or use the web UI's “安装 / 检查 Lightroom 插件” button.

The plug-in talks to the local service at `http://127.0.0.1:8000` (copy
`Config.local.example.lua` to `Config.local.lua` and change `SERVER_URL` if
needed), uploads a JPEG preview of the selected
photo, shows the analysis and style picker, and applies the returned
`applyDevelopSettings` table to the selected photo. It writes an
`AI Photo Agent - Before` snapshot first and switches to the Develop module.
Virtual-copy mode is currently disabled because `createVirtualCopy` blocked on
the tested Lightroom Classic 15.1 Windows build.

Menu location: `File > Plug-in Extras` (any module, recommended) or
`Library > Plug-in Extras` (Library module only).

- `AI Photo Agent - 分析并修图（当前照片）`
- `AI Photo Agent - 应用 Web 端待处理修图`
- `AI Photo Agent - 连接与安装状态`
- `AI Photo Agent - 打开 Web 修图页面`
- `AI Photo Agent - 显示分析窗口`（评审窗口被挡住或切走后，点它重新弹出）

See `docs/LIGHTROOM_PLUGIN.zh-CN.md` in the project root for the full workflow,
bridge directory layout, acceptance checklist, and limitations.

Windows only: `ReviewWindowTopmost.ps1` is started automatically before each
review. Lightroom 15.1 floating dialogs are not always-on-top, so the helper
keeps the review window above the main window while Lightroom is foreground,
removes its minimize button and exits when the window is gone. Disable it with
`Config.KEEP_REVIEW_WINDOW_ON_TOP = false`; the menu recall still works.
