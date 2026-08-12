export interface Shape {}
export type Identifier = string
export enum State { Ready }
export class Service {}
export function createService() { return new Service() }
export const factory = () => createService()
export default function() {}
export const resolver = function() { return factory() }
