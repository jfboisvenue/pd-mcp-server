def stats(*xs):
    """Recoit des nombres depuis Pd et retourne [somme max min moyenne]."""
    nums = [float(x) for x in xs]
    if not nums:
        return [0, 0, 0, 0]
    total = sum(nums)
    return [total, max(nums), min(nums), total / len(nums)]
