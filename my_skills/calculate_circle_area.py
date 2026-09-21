import math


def calculate_circle_area(radius: float) -> float:
    """计算圆的面积。

    根据给定的半径，使用公式 S = π * r² 计算圆的面积。

    Args:
        radius (float): 圆的半径，必须为非负数。

    Returns:
        float: 圆的面积，保留完整浮点精度。

    Raises:
        ValueError: 当半径小于 0 时抛出。

    Examples:
        >>> calculate_circle_area(5)
        78.53981633974483
        >>> calculate_circle_area(0)
        0.0
    """
    if radius < 0:
        raise ValueError("半径不能为负数")
    return math.pi * radius ** 2
