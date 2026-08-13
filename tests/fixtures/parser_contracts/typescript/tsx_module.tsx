export interface Card {
  title: string;
}

export function render(card: Card): string {
  return card.title;
}
