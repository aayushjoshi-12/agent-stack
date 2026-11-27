/** Images attached to a message (P44): checked here, sent as base64, previewed by object URL. */

export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const MAX_IMAGES = 8;

export type Attachment = { name: string; data: string; url: string };

/** Why a file cannot be attached, or null when it can. */
export function imageProblem(file: { name: string; type: string; size: number }): string | null {
  if (!IMAGE_TYPES.includes(file.type)) return `${file.name} is not a PNG, JPEG, WebP or GIF image.`;
  if (file.size > MAX_IMAGE_BYTES) return `${file.name} is over 5 MB.`;
  return null;
}

/** The base64 payload of a `data:<type>;base64,<payload>` URL. */
export function base64Of(dataUrl: string): string {
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

/** Read a picked, pasted or dropped file into an attachment. */
export function readImage(file: File): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Cannot read ${file.name}.`));
    reader.onload = () =>
      resolve({ name: file.name || "pasted.png", data: base64Of(String(reader.result)), url: URL.createObjectURL(file) });
    reader.readAsDataURL(file);
  });
}
