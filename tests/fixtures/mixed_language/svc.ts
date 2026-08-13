export interface User {
  id: number;
  name: string;
}

export function load(u: User) {
  return u.id;
}
