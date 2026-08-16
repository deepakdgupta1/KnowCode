def outer():
    def duplicate():
        helper()

    duplicate()


class Container:
    def method(self):
        def duplicate():
            nested_helper()

        duplicate()
