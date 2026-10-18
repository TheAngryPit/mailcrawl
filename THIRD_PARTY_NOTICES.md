# Third-party notices

mailcrawl does not vendor these libraries or their weights. Installing an extra downloads that library from its upstream. A model download is a separate step. This file names each one and links the upstream licence. It does not copy those terms into the tree.

mailcrawl does not redistribute any row below. An Apache NOTICE stays upstream for that reason. The MIT-CMU notice for Pillow stays upstream for the same reason. Copying either text would be required only in a distribution that actually ships that work.

The rows are the optional extras in `pyproject.toml`, the models those extras download, `torch` and `transformers` because the embed path names them, Poppler because extract and CI name `pdftotext`, and Jev because the code calls that API. Transitive numerical libraries are not listed.

| Name | Upstream | Licence id | Redistributed |
| --- | --- | --- | --- |
| sqlite-vec 0.1.9 | https://github.com/asg017/sqlite-vec/blob/v0.1.9/LICENSE-MIT and https://github.com/asg017/sqlite-vec/blob/v0.1.9/LICENSE-APACHE | MIT OR Apache-2.0 | no |
| sentence-transformers 6.1.0 | https://github.com/huggingface/sentence-transformers/blob/v6.1.0/LICENSE | Apache-2.0 | no |
| Pillow | https://github.com/python-pillow/Pillow/blob/main/LICENSE | MIT-CMU | no |
| MLX | https://github.com/ml-explore/mlx/blob/main/LICENSE | MIT | no |
| mlx-vlm at `3d87e88402f307efbf68e568971aa887ee7d9ed0` | https://github.com/Blaizzy/mlx-vlm/blob/3d87e88402f307efbf68e568971aa887ee7d9ed0/LICENSE | MIT | no |
| torch 2.14.1 | https://github.com/pytorch/pytorch/blob/v2.14.1/LICENSE and https://github.com/pytorch/pytorch/blob/v2.14.1/NOTICE | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | no |
| transformers 5.20.1 | https://github.com/huggingface/transformers/blob/v5.20.1/LICENSE | Apache-2.0 | no |
| EmbeddingGemma 2 (`google/embeddinggemma-2`) | https://huggingface.co/google/embeddinggemma-2 | Apache-2.0, plus the Gemma use policy below | no |
| `mlx-community/embeddinggemma-2-8bit` | https://huggingface.co/mlx-community/embeddinggemma-2-8bit | Apache-2.0, plus the Gemma use policy below | no |
| laya | https://pypi.org/project/laya/ and https://huggingface.co/convaiinnovations/laya | Apache-2.0 | no |
| laya-multilingual (`convaiinnovations/laya-multilingual`) | https://huggingface.co/convaiinnovations/laya-multilingual | Apache-2.0 | no |
| Poppler `pdftotext` | https://gitlab.freedesktop.org/poppler/poppler/-/blob/master/COPYING and https://gitlab.freedesktop.org/poppler/poppler/-/blob/master/COPYING3 | (GPL-2.0-only OR GPL-3.0-only) AND GPL-2.0-or-later AND LGPL-2.0-or-later AND LGPL-2.1-or-later AND MIT | no |
| Jev (`jev-1.13.0`) | https://typesafe.ai/legal/terms | none. The fetched page is site terms, not an SPDX licence | no |

Apache-2.0 text for the rows that use it is https://www.apache.org/licenses/LICENSE-2.0.txt. The EmbeddingGemma 2 repository and the Laya checkpoint repository do not ship a `LICENSE` file. Their model cards set `license` to `apache-2.0`.

Fedora records the Poppler expression on line 21 of https://src.fedoraproject.org/rpms/poppler/raw/rawhide/f/poppler.spec. `COPYING` is GPL version 2. `COPYING3` is GPL version 3. README-XPDF says the Xpdf parts are GPL v2 only, GPL v3 only, or GPL v2 or v3, and not any later version. mailcrawl only invokes the user's installed `pdftotext` as a subprocess. It does not link Poppler and it does not ship it.

## Gemma use policy

EmbeddingGemma 2 and the MLX 8-bit weights are Gemma models. The model cards list Apache-2.0. Deployments must adhere to the Gemma Prohibited Use Policy at https://ai.google.dev/gemma/prohibited_use_policy. The page fetched for this file says it was last modified on 21 February 2024.

That policy says you may not use Gemma or a model derivative for the uses it lists. The list includes child sexual abuse content, illegal activity, scams and phishing, bypassing safety filters, tracking or monitoring people without consent, and generating or inferring sensitive personal information without the rights the law requires. mailcrawl does not grant any right the policy withholds. Read the policy before you download the weights.
