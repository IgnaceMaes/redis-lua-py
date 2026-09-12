# The supported subset

A script body is Python that Python never runs, so only the part of the
language with a faithful Lua meaning is accepted.

**Supported:** assignment, augmented assignment, `if`/`elif`/`else`,
`for ... in` over a table or `range()`, `while`, `break`, `return`,
comparisons, arithmetic, f-strings, list and dict literals, `len()`,
`.append()`, `int()`, `float()`, `str()`, `min()`, `max()`, `abs()`,
module-level constants, and calls into `redis` and `cjson`.

Everything else raises
[`UnsupportedSyntax`](errors.md#unsupportedsyntax) when the module is imported,
with a caret under the line at fault:

```
'and'/'or' are only supported in an if or while condition
  File "/srv/app/limits.py", line 12
    flag = a and b
           ^
  hint: In Python these return an operand, which does not survive the
  difference in truthiness. Use an if statement instead.
```

Failing at import, loudly, is deliberate. A body that looks like Python but is
never run by Python is exactly where a quiet mistranslation would cost the
most.

The gaps that a supported construct still carries — truthiness, 1-based
indexing, block scope — are covered in
[Where Lua differs from Python](lua-vs-python.md).
