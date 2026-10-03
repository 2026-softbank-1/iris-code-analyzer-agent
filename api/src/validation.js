import { z } from 'zod';
export const uuidSchema = z.string().uuid();
export const orderSchema = z.object({
  userId: uuidSchema,
  items: z.array(z.object({ productId: uuidSchema, quantity: z.number().int().min(1).max(20) })).min(1).max(20),
}).superRefine((order, ctx) => {
  const ids = order.items.map((item) => item.productId);
  if (new Set(ids).size !== ids.length) ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'Product IDs must be unique' });
});
