export function jumpTo(elementId: string, block: ScrollLogicalPosition = "center") {
  const element = document.getElementById(elementId);
  if (element === null) return;
  element.scrollIntoView({ behavior: "smooth", block });
  element.classList.remove("pulse");
  void element.offsetWidth;
  element.classList.add("pulse");
}
