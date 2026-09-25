import { formatName } from "./utils/format";

export interface Greeter {
  greet(name: string): string;
}

export class Hello implements Greeter {
  greet(name: string): string {
    return `hi, ${formatName(name)}`;
  }
}

export const shout = (name: string): string => formatName(name).toUpperCase();
