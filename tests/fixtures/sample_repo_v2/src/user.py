class User:
    def __init__(self, name: str) -> None:
        self.name = name

    def greeting(self) -> str:
        return f"hi, {self.name}"

    def farewell(self) -> str:
        return f"bye, {self.name}"
