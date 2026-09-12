# The supported subset

A script body is Python that Python never runs, so only the part of the
language with a faithful Lua meaning is accepted.

**Supported:**

- **Statements:** assignment, including unpacking (`a, b = b, a`);
  augmented assignment; `if`/`elif`/`else`; `while`; `break`; `return`.
- **Loops:** `for ... in` over a table, `range()`, `enumerate()`, or a dict's
  `.items()`, `.keys()` and `.values()`, binding a name or a tuple of names.
- **Helper functions** defined with `def` at the top level of the body. They
  can call each other and themselves.
- **Expressions:** comparisons; arithmetic; `and`/`or`, and `a if c else b`,
  both as values; f-strings; list and dict literals.
- **Builtins and methods:** `len()`, `int()`, `float()`, `str()`, `min()`,
  `max()`, `abs()`, `.append()`, `.insert()`, `.pop()` and `str.join()`.
- **The `math` module:** `floor`, `ceil`, `sqrt`, `fabs`, `fmod`, `exp`,
  `log`, `log10` and `pow`, imported either way.
- **Calls:** into `redis` and `cjson`, with `*xs` allowed as the last argument.
- **Parameters:** `list[Key]` and `list[...]`, for a variable number of keys
  and arguments.
- **Module-level constants.**

Everything else raises
[`UnsupportedSyntax`](errors.md#unsupportedsyntax) when the module is imported,
with a caret under the line at fault:

```
Lua 5.1 has no 'continue' statement
  File "/srv/app/limits.py", line 12
    continue
    ^
  hint: Invert the condition and put the rest of the loop body inside the if.
```

Failing at import, loudly, is deliberate. A body that looks like Python but is
never run by Python is exactly where a quiet mistranslation would cost the
most.

The gaps that a supported construct still carries — truthiness, 1-based
indexing, block scope — are covered in
[Where Lua differs from Python](lua-vs-python.md).
