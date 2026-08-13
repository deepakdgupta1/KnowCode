class Base:
    def hello(self):
        return "hi"


class Sub(Base):
    def hello(self):
        return self.helper()

    def helper(self):
        return 1
