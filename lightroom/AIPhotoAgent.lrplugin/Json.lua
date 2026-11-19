-- Minimal JSON encoder/decoder for Lightroom Classic's Lua 5.1 runtime.
-- The service emits UTF-8 JSON with ensure_ascii=false, so non-ASCII text is
-- passed through. \uXXXX escapes are still decoded for portability.

local Json = {}

local function utf8_encode(code)
    if code < 0x80 then
        return string.char(code)
    elseif code < 0x800 then
        return string.char(
            0xC0 + math.floor(code / 0x40),
            0x80 + (code % 0x40)
        )
    elseif code < 0x10000 then
        return string.char(
            0xE0 + math.floor(code / 0x1000),
            0x80 + (math.floor(code / 0x40) % 0x40),
            0x80 + (code % 0x40)
        )
    end
    return string.char(
        0xF0 + math.floor(code / 0x40000),
        0x80 + (math.floor(code / 0x1000) % 0x40),
        0x80 + (math.floor(code / 0x40) % 0x40),
        0x80 + (code % 0x40)
    )
end

local function skip_whitespace(text, index)
    while index <= #text do
        local char = text:sub(index, index)
        if char ~= " " and char ~= "\t" and char ~= "\r" and char ~= "\n" then
            break
        end
        index = index + 1
    end
    return index
end

local parse_value

local function parse_string(text, index)
    if text:sub(index, index) ~= '"' then
        error("expected a JSON string at byte " .. tostring(index))
    end
    index = index + 1
    local parts = {}
    local segment_start = index
    while index <= #text do
        local char = text:sub(index, index)
        if char == '"' then
            parts[#parts + 1] = text:sub(segment_start, index - 1)
            return table.concat(parts), index + 1
        elseif char == "\\" then
            parts[#parts + 1] = text:sub(segment_start, index - 1)
            local escape = text:sub(index + 1, index + 1)
            if escape == '"' or escape == "\\" or escape == "/" then
                parts[#parts + 1] = escape
                index = index + 2
            elseif escape == "b" then
                parts[#parts + 1] = "\b"
                index = index + 2
            elseif escape == "f" then
                parts[#parts + 1] = "\f"
                index = index + 2
            elseif escape == "n" then
                parts[#parts + 1] = "\n"
                index = index + 2
            elseif escape == "r" then
                parts[#parts + 1] = "\r"
                index = index + 2
            elseif escape == "t" then
                parts[#parts + 1] = "\t"
                index = index + 2
            elseif escape == "u" then
                local hex = text:sub(index + 2, index + 5)
                local code = tonumber(hex, 16)
                if not code then
                    error("invalid \\u escape at byte " .. tostring(index))
                end
                index = index + 6
                if code >= 0xD800 and code <= 0xDBFF and text:sub(index, index + 1) == "\\u" then
                    local low = tonumber(text:sub(index + 2, index + 5), 16)
                    if low and low >= 0xDC00 and low <= 0xDFFF then
                        code = 0x10000 + ((code - 0xD800) * 0x400) + (low - 0xDC00)
                        index = index + 6
                    end
                end
                parts[#parts + 1] = utf8_encode(code)
            else
                error("unsupported JSON escape at byte " .. tostring(index))
            end
            segment_start = index
        else
            index = index + 1
        end
    end
    error("unterminated JSON string")
end

local function parse_object(text, index)
    local result = {}
    index = skip_whitespace(text, index + 1)
    if text:sub(index, index) == "}" then
        return result, index + 1
    end
    while true do
        local key
        key, index = parse_string(text, index)
        index = skip_whitespace(text, index)
        if text:sub(index, index) ~= ":" then
            error("expected ':' in JSON object at byte " .. tostring(index))
        end
        index = skip_whitespace(text, index + 1)
        local value
        value, index = parse_value(text, index)
        result[key] = value
        index = skip_whitespace(text, index)
        local separator = text:sub(index, index)
        if separator == "," then
            index = skip_whitespace(text, index + 1)
        elseif separator == "}" then
            return result, index + 1
        else
            error("expected ',' or '}' in JSON object at byte " .. tostring(index))
        end
    end
end

local function parse_array(text, index)
    local result = {}
    index = skip_whitespace(text, index + 1)
    if text:sub(index, index) == "]" then
        return result, index + 1
    end
    while true do
        local value
        value, index = parse_value(text, index)
        result[#result + 1] = value
        index = skip_whitespace(text, index)
        local separator = text:sub(index, index)
        if separator == "," then
            index = skip_whitespace(text, index + 1)
        elseif separator == "]" then
            return result, index + 1
        else
            error("expected ',' or ']' in JSON array at byte " .. tostring(index))
        end
    end
end

parse_value = function(text, index)
    index = skip_whitespace(text, index)
    local char = text:sub(index, index)
    if char == "{" then
        return parse_object(text, index)
    elseif char == "[" then
        return parse_array(text, index)
    elseif char == '"' then
        return parse_string(text, index)
    elseif text:sub(index, index + 3) == "true" then
        return true, index + 4
    elseif text:sub(index, index + 4) == "false" then
        return false, index + 5
    elseif text:sub(index, index + 3) == "null" then
        return nil, index + 4
    end

    local number = text:match("^-?%d+%.?%d*[eE]?[+-]?%d*", index)
    if number and #number > 0 then
        local value = tonumber(number)
        if value == nil then
            error("invalid JSON number at byte " .. tostring(index))
        end
        return value, index + #number
    end
    error("unexpected JSON token at byte " .. tostring(index))
end

function Json.decode(text)
    if type(text) ~= "string" then
        error("JSON.decode expects a string")
    end
    local value, index = parse_value(text, 1)
    index = skip_whitespace(text, index)
    if index <= #text then
        error("trailing data after JSON document at byte " .. tostring(index))
    end
    return value
end

local function encode_string(value)
    local escaped = value:gsub('[%z\1-\31\\"]', function(char)
        local map = {
            ['"'] = '\\"',
            ["\\"] = "\\\\",
            ["\b"] = "\\b",
            ["\f"] = "\\f",
            ["\n"] = "\\n",
            ["\r"] = "\\r",
            ["\t"] = "\\t",
        }
        return map[char] or string.format("\\u%04x", string.byte(char))
    end)
    return '"' .. escaped .. '"'
end

local function is_array(value)
    local count = 0
    local maximum = 0
    for key in pairs(value) do
        if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then
            return false
        end
        count = count + 1
        if key > maximum then
            maximum = key
        end
    end
    return count == maximum
end

local encode_value

encode_value = function(value, seen)
    local value_type = type(value)
    if value == nil then
        return "null"
    elseif value_type == "boolean" then
        return value and "true" or "false"
    elseif value_type == "number" then
        if value ~= value or value == math.huge or value == -math.huge then
            return "null"
        end
        if value % 1 == 0 and math.abs(value) < 1e15 then
            return string.format("%.0f", value)
        end
        return string.format("%.14g", value)
    elseif value_type == "string" then
        return encode_string(value)
    elseif value_type ~= "table" then
        error("cannot encode a " .. value_type .. " as JSON")
    end

    if seen[value] then
        error("cannot encode a cyclic table as JSON")
    end
    seen[value] = true

    local parts = {}
    if is_array(value) then
        for index = 1, #value do
            parts[#parts + 1] = encode_value(value[index], seen)
        end
        seen[value] = nil
        return "[" .. table.concat(parts, ",") .. "]"
    end

    local keys = {}
    for key in pairs(value) do
        if type(key) ~= "string" then
            error("JSON object keys must be strings")
        end
        keys[#keys + 1] = key
    end
    table.sort(keys)
    for _, key in ipairs(keys) do
        parts[#parts + 1] = encode_string(key) .. ":" .. encode_value(value[key], seen)
    end
    seen[value] = nil
    return "{" .. table.concat(parts, ",") .. "}"
end

function Json.encode(value)
    return encode_value(value, {})
end

return Json
