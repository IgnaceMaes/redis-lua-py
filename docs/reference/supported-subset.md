# The supported subset

A script body is Python that Python never runs, so only the part of the
language with a faithful Lua meaning is accepted.

**Supported:**

- **Statements:** assignment, including unpacking (`a, b = b, a`);
  augmented assignment; `if`/`elif`/`else`; `while`; `break`; `continue`;
  `return`.
- **Errors:** `try` with one `except` (bare or `Exception`), `else` and
  `finally`; `raise SomeError("message")`, and a bare `raise` inside `except`;
  `assert`.
- **Loops:** `for ... in` over a table, `range()`, `enumerate()`, or a dict's
  `.items()`, `.keys()` and `.values()`, binding a name or a tuple of names.
- **Helper functions** defined with `def` at the top level of the body, and
  `lambda` wherever an expression can go. They can call each other and
  themselves, and be passed to a helper that calls them.
- **Expressions:** comparisons, chained ones such as `0 < n <= limit`
  included; arithmetic; `in` and `not in`; `and`/`or`, and `a if c else b`,
  both as values; list and dict literals.
- **Subscripts:** indices, dict keys of any type, negative literal indices on
  a name, and slices of lists and strings, with a step or without.
- **Strings:** f-strings with format specs; `%` formatting; `+` and `*` on a
  string; `str.join`, `upper`, `lower`, `strip`, `lstrip`, `rstrip`,
  `startswith`, `endswith`, `find`, `split` and `replace`; `encode()` and
  `decode()` in UTF-8, which leave the bytes as they are.
- **Builtins and methods:** `len()`, `int()`, which truncates toward zero,
  `float()`, `str()`, `min()`, `max()`, `abs()`, `ord()`, `chr()`, `.append()`,
  `.insert()`, `.pop()` and `dict.get()`; `isinstance()` against `str`,
  `bytes`, `int`, `float`, `bool`, `dict` and `list`.
- **The `math` module:** `floor`, `ceil`, `sqrt`, `fabs`, `fmod`, `exp`,
  `log`, `log10` and `pow`, imported either way.
- **Calls:** into `redis` and `cjson`, with `*xs` allowed as the last argument,
  and `cjson.null` for a JSON null.
- **Parameters:** `list[Key]` and `list[...]`, for a variable number of keys
  and arguments.
- **Module-level constants.**

Everything else raises
[`UnsupportedSyntax`](errors.md#unsupportedsyntax) when the module is imported,
with a caret under the line at fault:

```
math.log() with a base is not supported
  File "/srv/app/limits.py", line 12
    buckets = math.log(n, 2)
              ^
  hint: Lua 5.1's math.log takes no base; divide by math.log(base) instead.
```

Failing at import, loudly, is deliberate. A body that looks like Python but is
never run by Python is exactly where a quiet mistranslation would cost the
most.

The gaps that a supported construct still carries — truthiness, 1-based
indexing, block scope — are covered in
[Where Lua differs from Python](lua-vs-python.md).
