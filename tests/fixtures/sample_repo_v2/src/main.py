from src.utils.math_tools import add
from src.user import User


class Calculator:
    def __init__(self, initial: int = 0) -> None:
        self.value = initial

    def add(self, x: int) -> int:
        self.value = add(self.value, x)
        return self.value


def run() -> None:
    calc = Calculator()
    calc.add(5)
    user = User("bob")
    print(user.greeting(), calc.value)


if __name__ == "__main__":
    run()
