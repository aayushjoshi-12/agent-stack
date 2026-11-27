import assert from "node:assert/strict";
import { test } from "node:test";
import { base64Of, imageProblem, MAX_IMAGE_BYTES } from "./images.ts";

test("only small PNG, JPEG, WebP and GIF files can be attached", () => {
  assert.equal(imageProblem({ name: "a.png", type: "image/png", size: 10 }), null);
  assert.equal(imageProblem({ name: "b.webp", type: "image/webp", size: MAX_IMAGE_BYTES }), null);
  assert.match(imageProblem({ name: "c.pdf", type: "application/pdf", size: 10 }) ?? "", /not a PNG/);
  assert.match(imageProblem({ name: "d.svg", type: "image/svg+xml", size: 10 }) ?? "", /not a PNG/);
  assert.match(imageProblem({ name: "e.jpg", type: "image/jpeg", size: MAX_IMAGE_BYTES + 1 }) ?? "", /over 5 MB/);
});

test("the base64 payload is what follows the comma of a data URL", () => {
  assert.equal(base64Of("data:image/png;base64,iVBORw0K"), "iVBORw0K");
});
