import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { navigate } from "../lib/router";

export function Link({ to, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  }
  return <a {...props} href={to} onClick={onClick} />;
}
