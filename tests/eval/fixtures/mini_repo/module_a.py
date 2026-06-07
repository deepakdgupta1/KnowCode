from tests.eval.fixtures.mini_repo.module_b import func_b

def func_a():
    """Trace dependencies from module_a to module_b (mini_repo fixture)."""
    return func_b()
