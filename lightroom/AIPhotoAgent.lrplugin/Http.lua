-- HTTP helpers for the local AI Photo Agent service.

local LrHttp = import "LrHttp"
local LrPathUtils = import "LrPathUtils"

local Json = dofile(LrPathUtils.child(_PLUGIN.path, "Json.lua"))
local Config = dofile(LrPathUtils.child(_PLUGIN.path, "Config.lua"))

local Http = {}

local function endpoint(path)
    local base = Config.SERVER_URL:gsub("/+$", "")
    return base .. path
end

local function decode_response(body, headers)
    local status = headers and headers.status or 0
    local decoded = nil
    if body and #body > 0 then
        local ok, value = pcall(Json.decode, body)
        if ok then
            decoded = value
        end
    end
    if status == 0 then
        -- LrHttp reports a transport failure as status 0. The usual cause is
        -- simply that the local service is not running, so say that instead of
        -- surfacing a bare "请求失败" the user cannot act on.
        return nil, string.format(
            "无法连接本地服务（%s）。请先启动 AI Photo Agent 服务：在项目目录运行 "
                .. "`.venv\\Scripts\\python.exe run_app.py --no-browser --port 8000`，"
                .. "然后重试。",
            endpoint("")
        )
    end
    if status < 200 or status >= 300 then
        local detail = nil
        if type(decoded) == "table" and type(decoded.detail) == "string" then
            detail = decoded.detail
        elseif body and #body > 0 then
            detail = body:sub(1, 800)
        end
        return nil, string.format("HTTP %s: %s", tostring(status), detail or "请求失败")
    end
    if decoded == nil then
        return nil, "服务器返回了无法解析的 JSON。"
    end
    return decoded
end

function Http.get(path)
    return Http.get_with_timeout(path, Config.STATUS_TIMEOUT)
end

function Http.get_with_timeout(path, timeout)
    local body, headers = LrHttp.get(endpoint(path), nil, timeout)
    return decode_response(body, headers)
end

function Http.post_json(path, payload)
    local body, headers = LrHttp.post(
        endpoint(path),
        Json.encode(payload),
        { { field = "Content-Type", value = "application/json" } },
        "POST",
        Config.REQUEST_TIMEOUT
    )
    return decode_response(body, headers)
end

function Http.post_json_with_timeout(path, payload, timeout)
    local body, headers = LrHttp.post(
        endpoint(path),
        Json.encode(payload),
        { { field = "Content-Type", value = "application/json" } },
        "POST",
        timeout
    )
    return decode_response(body, headers)
end

function Http.post_empty(path)
    local body, headers = LrHttp.post(
        endpoint(path),
        "",
        nil,
        "POST",
        Config.REQUEST_TIMEOUT
    )
    return decode_response(body, headers)
end

-- Uploads one image as the multipart "file" field. The service sniffs the
-- container from magic bytes, so the file name and content type are only hints.
function Http.upload_image(image_data, file_name, content_type)
    local body, headers = LrHttp.postMultipart(
        endpoint("/api/photos"),
        {
            {
                name = "file",
                fileName = file_name or "lightroom-preview.jpg",
                value = image_data,
                contentType = content_type or "image/jpeg",
            },
        },
        nil,
        Config.UPLOAD_TIMEOUT
    )
    return decode_response(body, headers)
end

function Http.upload_jpeg(jpeg_data)
    return Http.upload_image(jpeg_data, "lightroom-preview.jpg", "image/jpeg")
end

return Http
