"""The Lua helpers a script is given when, and only when, its body calls them."""

from __future__ import annotations

from collections.abc import Iterable

# Runtime helpers, emitted only when a script uses them, in this order.
HELPERS: dict[str, str] = {
    "__truthy": """\
-- Python truthiness: 0, '', empty tables and nil are all false.
local function __truthy(v)
  if v == nil or v == false then return false end
  if v == 0 or v == '' then return false end
  if type(v) == 'table' and next(v) == nil then return false end
  return true
end""",
    "__isnil": """\
-- A Redis command with nothing to return hands Lua false, not nil, so an
-- `is None` test has to accept both. Taking v as an argument also means the
-- operand is evaluated once, not once per comparison.
local function __isnil(v)
  return v == nil or v == false
end""",
    "__or": """\
-- Python's `a or b`: a when it is truthy by Python's rules, otherwise b.
local function __or(a, b)
  if __truthy(a) then return a end
  return b
end""",
    "__and": """\
-- Python's `a and b`: b when a is truthy by Python's rules, otherwise a.
local function __and(a, b)
  if __truthy(a) then return b end
  return a
end""",
    "__errmsg": """\
-- The message of a caught error. Redis hands pcall a string, but a table with
-- an err field or a userdata are possible too, so each is turned into one.
local function __errmsg(e)
  if type(e) == 'table' and e.err ~= nil then return e.err end
  return tostring(e)
end""",
    "__key": """\
-- A subscript only known at runtime: a number is a 0-based position, and
-- anything else is a dict key, used as it is.
local function __key(k)
  if type(k) == 'number' then return k + 1 end
  return k
end""",
    "__slice": """\
-- Python's v[i:j], for a string or a list: bounds count from 0, a negative
-- bound counts back from the end, and a missing one goes all the way.
local function __slice(v, i, j)
  local n = #v
  if i == nil then i = 0 elseif i < 0 then i = math.max(n + i, 0) elseif i > n then i = n end
  if j == nil then j = n elseif j < 0 then j = math.max(n + j, 0) elseif j > n then j = n end
  if type(v) == 'string' then return string.sub(v, i + 1, j) end
  local out = {}
  for k = i + 1, j do out[#out + 1] = v[k] end
  return out
end""",
    "__slicestep": """\
-- Python's v[i:j:k], for a string or a list: every k-th item from i up to j,
-- or down to it when k is negative, with the bounds clamped as Python does.
local function __slicestep(v, i, j, k)
  if k == 0 then error('slice step cannot be zero', 0) end
  local n = #v
  -- Walking down, -1 stands for the position before the first item.
  local low, high = 0, n
  if k < 0 then low, high = -1, n - 1 end
  local function bound(b, default)
    if b == nil then return default end
    if b < 0 then b = b + n end
    return math.min(math.max(b, low), high)
  end
  i = bound(i, k > 0 and 0 or n - 1)
  j = bound(j, k > 0 and n or -1)
  local stop = k > 0 and j - 1 or j + 1
  local out = {}
  if type(v) == 'string' then
    for p = i, stop, k do out[#out + 1] = string.sub(v, p + 1, p + 1) end
    return table.concat(out)
  end
  for p = i, stop, k do out[#out + 1] = v[p + 1] end
  return out
end""",
    "__contains": """\
-- Python's `x in c`, where c is only known at runtime: a substring of a string,
-- and otherwise an element of a list or a key of a dict. Lua has one table for
-- both, so a table with any key besides the positions 1 to #c is a dict.
local function __contains(c, x)
  if type(c) == 'string' then return string.find(c, x, 1, true) ~= nil end
  local n, found = #c, false
  for k, v in pairs(c) do
    if type(k) ~= 'number' or k < 1 or k > n or k % 1 ~= 0 then return c[x] ~= nil end
    if v == x then found = true end
  end
  return found
end""",
    "__inlist": """\
-- Python's `x in xs`, for a list.
local function __inlist(xs, x)
  for i = 1, #xs do
    if xs[i] == x then return true end
  end
  return false
end""",
    "__dictlen": """\
-- len() of a dict, whose keys Lua's # does not count.
local function __dictlen(d)
  local n = 0
  for _ in pairs(d) do n = n + 1 end
  return n
end""",
    "__add": """\
-- Python's a + b, where neither side is known to be a number or a string: two
-- strings are joined, two lists are joined into a new one, and the rest added.
local function __add(a, b)
  local ta, tb = type(a), type(b)
  if ta == 'string' and tb == 'string' then return a .. b end
  if ta == 'table' and tb == 'table' then
    local out, n = {}, #a
    for i = 1, n do out[i] = a[i] end
    for i = 1, #b do out[n + i] = b[i] end
    return out
  end
  return a + b
end""",
    "__startswith": """\
local function __startswith(s, prefix)
  return string.sub(s, 1, #prefix) == prefix
end""",
    "__endswith": """\
local function __endswith(s, suffix)
  return suffix == '' or string.sub(s, -#suffix) == suffix
end""",
    "__find": """\
-- str.find: the 0-based position of a plain substring, or -1.
local function __find(s, sub)
  local i = string.find(s, sub, 1, true)
  if i == nil then return -1 end
  return i - 1
end""",
    "__split": """\
-- str.split: on a plain separator, or on runs of whitespace without one.
local function __split(s, sep)
  local out = {}
  if sep == nil then
    for part in string.gmatch(s, '%S+') do out[#out + 1] = part end
    return out
  end
  if sep == '' then error('empty separator', 0) end
  local start = 1
  while true do
    local i, j = string.find(s, sep, start, true)
    if i == nil then break end
    out[#out + 1] = string.sub(s, start, i - 1)
    start = j + 1
  end
  out[#out + 1] = string.sub(s, start)
  return out
end""",
    "__replace": """\
-- str.replace, of a plain substring rather than a Lua pattern.
local function __replace(s, old, new)
  local out = {}
  if old == '' then
    out[1] = new
    for i = 1, #s do
      out[#out + 1] = string.sub(s, i, i)
      out[#out + 1] = new
    end
    return table.concat(out)
  end
  local start = 1
  while true do
    local i, j = string.find(s, old, start, true)
    if i == nil then break end
    out[#out + 1] = string.sub(s, start, i - 1)
    out[#out + 1] = new
    start = j + 1
  end
  out[#out + 1] = string.sub(s, start)
  return table.concat(out)
end""",
    "__get": """\
-- dict.get: the value under a key, or the default when there is none.
local function __get(t, k, default)
  local v = t[k]
  if v == nil then return default end
  return v
end""",
}


def helper_order(names: Iterable[str]) -> list[str]:
    """Helper names in the order they are emitted, without repeats."""
    wanted = set(names)
    return [helper for helper in HELPERS if helper in wanted]


def helper_source(helper: str) -> str:
    """The Lua source of a helper, by name."""
    return HELPERS[helper].rstrip()
