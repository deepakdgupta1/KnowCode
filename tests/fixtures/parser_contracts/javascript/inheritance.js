class Parent {}
class Child extends Parent {}
class RemoteChild extends Framework.Component {}
class MixedChild extends withMixin(Parent) {}
class ConditionalChild extends (enabled ? Parent : Framework.Component) {}
class ForwardChild extends ForwardParent {}
class ForwardParent {}
