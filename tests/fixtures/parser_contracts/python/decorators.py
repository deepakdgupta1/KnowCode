@register("service")
@trace
class Service:
    @cached
    def run(self):
        return 1
