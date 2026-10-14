---
title: "Janas: Run LLMs larger than RAM by streaming experts from SSD"
description: "You have a 32 GB laptop. You want to run a 48 GB model. And you want it to feel fast."
pubDate: "2026-10-14"
tags: ["Cloud & DevOps", "News & Releases", "AI Tools", "Gadgets & Hardware"]
keywords: ["run LLMs larger than RAM", "LLM inference from SSD", "Janas streaming LLM architecture", "memory efficient LLM inference", "LLM inference without GPU memory"]
author: "AutoBlog"
lang: "en"
heroImage: "/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd/image-1.jpg"
heroImageAlt: "Janas: Run LLMs larger than RAM by streaming experts from SSD"
draft: false
---

You have a 32 GB laptop. You want to run a 48 GB model. And you want it to feel fast.

Sounds like a contradiction. But a new tool called **Janas** just made it a Tuesday-afternoon benchmark run. On a standard consumer laptop with no discrete GPU, no 64 GB of RAM, and no cloud API key, Janas pushed Qwen3-Next-80B to **37 tokens per second** (24 without speculative drafts). For context, llama.cpp managed 2.8 tok/s on the exact same machine for the same model.

That's not a typo. That's a 13× difference in generation speed, and a 4× difference in prompt reading (62 vs. 16 tok/s).

If you've ever wanted to run LLMs larger than RAM without renting a server or begging your IT department for a workstation, this is the post for you.

## What Janas Actually Does

Janas is a **pure C** inference engine designed around one core idea: you don't need the entire model in RAM (or VRAM) at once. Instead, it **streams experts from your NVMe SSD** on the fly, feeding just the active weights to the CPU or integrated GPU as each token is generated.

Think of it like this. Traditional inference loads the whole model into memory. If the model is 48 GB and your RAM is 32 GB, you're stuck—either swap to disk (painfully slow) or quantize harder (losing quality). Janas sidesteps the problem entirely. Its **Janas streaming LLM architecture** treats the SSD as a fast, always-available weight reservoir, pulling in only what the current computation step needs.

Key design points from the project:

- **Pure C implementation** — no Python runtime overhead, no PyTorch dependency, no CUDA toolkit required.
- **CPU or GPU** — it works on the integrated Arc GPU in Intel's Core Ultra chips, and falls back to CPU where the GPU path isn't optimized yet.
- **Memory efficient LLM inference** — the model file lives on the SSD; only working sets live in RAM/VRAM.
- **LLM inference without GPU memory** constraints — you're not fighting for VRAM against a browser with 47 tabs open.

The result is that a 48 GB model becomes a "laptop-sized" problem, provided your SSD is fast enough (and in the test setup, it was an NVMe drive).


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd/image-2.jpg)


## The Numbers That Matter

The benchmarks come from a single, well-specified machine:

- **OS:** Debian 13 Linux
- **CPU:** Intel Core Ultra 9 185H
- **GPU:** Integrated Intel Arc
- **RAM:** 32 GB
- **Storage:** NVMe SSD
- **Test window:** 9–12 October 2026

Here's what stood out:

- **Qwen3-Next-80B (48 GB):** Janas hit 37 tok/s generation (24 without drafts) and 62 tok/s prompt processing. llama.cpp on the same hardware: 2.8 tok/s generation, 16 tok/s prompt.
- **Across 13 models in Q4_K_M quantization:** Janas was faster than llama.cpp on every single one.
- **Qwen3.8-27B:** After a GPU submission optimization, Janas read prompts at 30.6 tok/s (up from 26.4 the day before, and 18.7 before the GPU tiles for IQ4_XS, IQ3_S, and IQ4_NL weights from unsloth's UD mix were ready). llama.cpp was measured on 12 October 2026 with `llama-bench -r 2`.
- **One honest exception:** the 3-bit Qwen3-30B reads prompts *slower* than llama.cpp, because its Q3_K weights don't yet route through the GPU's fast prompt path. However, it **writes three times as fast** as llama.cpp.

That last exception matters. It tells you the team is reporting where the tool *isn't* faster yet, which is a good sign of a project that isn't just marketing.


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd/image-3.jpg)


## Why This Changes the Local LLM Conversation

For most of the past two years, the practical advice for "I want a big model on my laptop" was one of three unappealing options:

1. **Quantize harder** (Q2, Q1) until it fits, accepting noticeable quality loss.
2. **Rent a cloud GPU** and pay per token, forever.
3. **Buy more RAM**, which on a laptop means buying a new laptop.

Janas introduces a fourth option: **keep the model at a reasonable quantization (Q4_K_M, IQ4_XS, etc.), keep it on the SSD, and stream it through.** You're not sacrificing quality to fit a memory budget. You're leveraging the one component in a modern laptop that has effectively unlimited capacity and very high sequential bandwidth: the NVMe SSD.

This is especially relevant if:

- You're a developer testing prompts against a 70B+ model before shipping to a cloud API.
- You're a researcher who needs to iterate on a large model offline (air-gapped environments, field work, etc.).
- You're a hobbyist who simply wants to *talk* to a big model at a pace that doesn't make you check your email while waiting.

The "24 tok/s without drafts" figure is the one to anchor on. That's roughly the speed of a human reading. You can type a prompt, hit enter, and watch the response stream in real time. On a 32 GB laptop. Running a 48 GB model.


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd/image-4.jpg)


## Practical Takeaways

If you want to try something like this:

- **Check your SSD speed first.** Janas is only as fast as your sequential read bandwidth. A SATA SSD will throttle the whole pipeline. NVMe is the floor.
- **You don't need a discrete GPU.** The benchmarks above used an integrated Arc GPU. The CPU path also works. This is LLM inference without GPU memory as a hard requirement.
- **Quantization still matters, but less.** You can stay at Q4_K_M or similar without the "will it fit?" anxiety, because the SSD is your overflow.
- **Watch for the exceptions.** As the Q3_K / 3-bit case shows, not every weight format has the fast path yet. Check the project's benchmark table before assuming parity.
- **It's pure C.** If you're comfortable compiling, the build should be straightforward. No virtual environments, no dependency hell.

## Where It Stands

Janas is a young project, and the benchmark window (9–12 October 2026) is narrow. The team is clearly iterating fast—prompt-reading speed on Qwen3.8-27B jumped from 18.7 to 30.6 tok/s within a couple of days as GPU tile support for specific quant formats landed.

What's not yet confirmed in the available materials: a full architectural writeup of the streaming scheduler, a comparison against other SSD-streaming approaches (like vLLM's offloading or llama.cpp's own `--n-gpu-layers` with partial offload), or a published license/roadmap beyond the GitHub repo. If you're evaluating it for production, dig into the source and the issues tracker.

But the headline fact doesn't need a roadmap to be impressive: **a 48 GB model, a 32 GB laptop, 24+ tok/s, pure C, no cloud.** That's a different category of "local LLM" than most of us have been able to touch.

## Sources

- [Janas GitHub repository](https://github.com/prabanta-dev/janas)