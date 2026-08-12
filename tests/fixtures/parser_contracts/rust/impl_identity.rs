trait Display {}
struct Point;
impl Point {
    fn new() -> Self { Point }
}
impl Display for Point {
    fn display(&self) {}
}
impl vendor::Render for Point {
    fn render(&self) {}
}
