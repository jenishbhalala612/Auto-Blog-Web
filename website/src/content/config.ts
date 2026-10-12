import { defineCollection, z } from 'astro:content';

const blog = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    updatedDate: z.coerce.date().optional(),
    heroImage: z.string().optional(),
    heroImageAlt: z.string().optional(),
    tags: z.array(z.string()).default([]),
    keywords: z.array(z.string()).default([]),
    author: z.string().default('AutoBlog'),
    lang: z.enum(['en', 'hi']).default('en'),
    draft: z.boolean().default(false),
  }),
});

export const collections = { blog };
