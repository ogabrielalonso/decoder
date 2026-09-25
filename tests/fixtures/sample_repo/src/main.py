from src.utils.math_tools import add, multiply
from src.user import User


class Calculator:
    def __init__(self, initial: int = 0) -> None:
        self.value = initial

    def add(self, x: int) -> int:
        self.value = add(self.value, x)
        return self.value

    def scale(self, factor: int) -> int:
        self.value = multiply(self.value, factor)
        return self.value


def run() -> None:
    calc = Calculator()
    calc.add(5)
    calc.scale(3)
    user = User("alice")
    print(user.greeting(), calc.value)


if __name__ == "__main__":
    run()
