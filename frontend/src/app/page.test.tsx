import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import HomePage from "./page";

test("home page shows the product name", () => {
  expect(renderToStaticMarkup(<HomePage />)).toContain("FlowBuilder");
});
