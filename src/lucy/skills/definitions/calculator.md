---
name: calculator
description: Perform precise mathematical calculations and logical reasoning
triggers: calculate|compute|math|solve|what is|evaluate|sqrt|log|sin|cos|tan|pi|factorial|binomial|derivative|integral
category: productivity
---

# Calculator & Logic Solver

## Purpose
Provides precise mathematical computation without relying on LLM estimation. Handles arithmetic, algebra, calculus, statistics, and logical reasoning.

## Usage
Trigger with natural language containing math expressions, or call directly:

```
calculate("2 + 2 * 3")
calculate("sqrt(16) + log(100)")
calculate("derivative(x^3 + 2*x^2 + 5, x)")
calculate("integrate(sin(x), x, 0, pi)")
```

## Implementation
Uses Python's `math` and `sympy` modules for symbolic and numerical computation.