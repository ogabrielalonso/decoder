class User:
    def __init__(self, name: str) -> None:
        self.name = name

    def greeting(self) -> str:
        return f"hello, {self.name}"
