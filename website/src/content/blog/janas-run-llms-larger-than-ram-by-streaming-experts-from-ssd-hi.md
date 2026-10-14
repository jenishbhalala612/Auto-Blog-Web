---
title: "Janas: Run LLMs larger than RAM by streaming experts from SSD"
description: "आपके पास 32 GB RAM वाला लैपटॉप है, और आप एक 48 GB का LLM चलाना चाहते हैं। लगता है असंभव? Janas कहता है — 'कोई बात नहीं, SSD से stream कर लेंगे।' और यह claim ..."
pubDate: "2026-10-14"
tags: ["AI Tools", "Cloud & DevOps", "Gadgets & Hardware"]
keywords: ["run LLMs larger than RAM", "LLM inference from SSD", "Janas streaming LLM architecture", "memory efficient LLM inference", "LLM inference without GPU memory"]
author: "AutoBlog"
lang: "hi"
heroImage: "/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd-hi/image-1.jpg"
heroImageAlt: "Janas: Run LLMs larger than RAM by streaming experts from SSD"
draft: false
---

आपके पास 32 GB RAM वाला लैपटॉप है, और आप एक 48 GB का LLM चलाना चाहते हैं। लगता है असंभव? Janas कहता है — "कोई बात नहीं, SSD से stream कर लेंगे।" और यह claim सिर्फ़ marketing buzzword नहीं है। Qwen3-Next-80B, एक 48 GB का मॉडल, इसी 32 GB RAM वाले लैपटॉप पर 37 tok/s की गति से generate कर रहा है। बिना किसी बाहरी GPU के। बिना cloud के। सिर्फ़ pure C, CPU और integrated GPU।

यही है **Janas streaming LLM architecture** का मूल विचार: जब RAM में पूरा model नहीं आता, तो experts को SSD से stream करें। यह **LLM inference from SSD** का एक नया, प्रैक्टिकल दृष्टिकोण है जो **memory efficient LLM inference** को एक नए स्तर पर ले जाता है।

## Janas क्या है और यह कैसे काम करता है

Janas-LLM एक pure C में लिखा गया inference engine है। इसका core idea simple है: LLM के experts (Mixture-of-Experts architecture में) को एक साथ RAM में load करने के बजाय, ज़रूरत पड़ने पर SSD से stream करें। इसका मतलब है कि आप **run LLMs larger than RAM** कर सकते हैं — model की size आपके physical memory से बड़ी हो सकती है, और फिर भी inference चलती रहेगी।

यह सिर्फ़ CPU पर काम करता है, या GPU (integrated हो ya discrete) पर। दोनों को साथ में भी use किया जा सकता है। कोई Python dependency नहीं, कोई heavy framework नहीं। सिर्फ़ C code, जो compile करके चल जाता है। इसका मतलब है कि **LLM inference without GPU memory** की एक practical path मिलती है — जहाँ आपकी integrated GPU के limited VRAM को पूरा model load करने की ज़रूरत नहीं पड़ती।


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd-hi/image-2.jpg)


## बेंचमार्क: 32 GB लैपटॉप पर 48 GB मॉडल

अब आते हैं असली numbers पर। Test machine थी:

- **OS:** Debian 13 Linux
- **CPU:** Intel Core Ultra 9 185H
- **GPU:** Integrated Intel Arc GPU
- **RAM:** 32 GB
- **Storage:** NVMe SSD
- **Testing window:** 9–12 October 2026

इसी machine पर Qwen3-Next-80B (48 GB, Q4_K_M quantization) के results:

| Metric | Janas-LLM | llama.cpp |
|--------|-----------|-----------|
| Generation (with drafts) | **37 tok/s** | 2.8 tok/s |
| Generation (without drafts) | **24 tok/s** | 2.8 tok/s |
| Prompt reading | **62 tok/s** | 16 tok/s |

यह numbers देखकर एक बात साफ़ है: Janas ने 48 GB model को 32 GB RAM वाले machine पर चलाया, और speed में llama.cpp से 8x से 13x तक का advantage लिया। 37 tok/s का मतलब है कि आप वाकई-वाकई typing speed से faster text generate कर सकते हैं — बिना किसी cloud API call के, बिना किसी external GPU के।


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd-hi/image-3.jpg)


## llama.cpp के मुकाबले: 13 मॉडलों की तुलना

Research context के अनुसार, Janas-LLM ने सभी 13 models की तुलना में llama.cpp से बेहतर performance दिखाई — और यह Q4_K_M quantization पर था। यानी यह कोई एक model की fluke नहीं है; यह एक systematic advantage है।

एक specific example: Qwen3.8-27B पर, llama.cpp को 12 October 2026 को `llama-bench -r 2` से measure किया गया। Janas-LLM को 13 October को `janas-bench --prompt 1024 --gen 16` (two rounds) से test किया गया। जब prompt पूरा एक ही submission में GPU पर गया, तो Janas 30.6 tok/s पर पहुँचा — जबकि एक दिन पहले यह 26.4 tok/s पर था, और GPU tiles (IQ4_XS, IQ3_S, IQ4_NL weights of unsloth's UD mix) optimize होने से पहले 18.7 tok/s पर था।

यह progression दिखाता है कि Janas की architecture में GPU utilization को incrementally improve किया जा सकता है, और हर optimization step पर speed बढ़ती है।


![Janas: Run LLMs larger than RAM by streaming experts from SSD](/images/posts/2026-10-14/janas-run-llms-larger-than-ram-by-streaming-experts-from-ssd-hi/image-4.jpg)


## एक अपवाद और तकनीकी न्यूance

हर story में एक exception होती है, और यहाँ भी है: 3-bit Qwen3-30B पर, Janas prompt reading में llama.cpp से धीमा है। कारण स्पष्ट है — Q3_K weights अभी GPU के fast prompt path पर नहीं जा रहे। लेकिन generation (writing) में Janas तब भी llama.cpp से तीन गुना तेज़ है।

यह transparency अच्छी बात है। Janas के developers ने यह limitation खुद document किया है, और यह बताता है कि यह project अभी evolving है, और Q3_K quantization का GPU support आने वाला है।

## निष्कर्ष: Local LLM Inference का नया अध्याय

Janas-LLM का contribution सिर्फ़ "fast inference" नहीं है। यह एक paradigm shift है: **LLM inference from SSD** को एक viable, practical path बनाना। जब तक NVMe SSDs की bandwidth 5,000-7,000 MB/s की range में है, experts को stream करना एक legitimate strategy बन सकता है — बशर्ते architecture सही हो, C-level optimization सही हो, और GPU tiles सही ढंग से use हों।

32 GB RAM वाले laptop पर 48 GB model चलाना — यह 2024 में science fiction लगता। 2026 में, यह एक GitHub repo है। और यह pure C में है।

अगर आप local LLMs पर काम करते हैं, और आपकी RAM model size से कम है, तो Janas-LLM को एक look देने का बिल्कुल वजह है।

## स्रोत

- https://github.com/prabanta-dev/janas