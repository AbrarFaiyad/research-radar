# Research digest, 2026-11-19

**Four materials-science claims lack identifiable primary sources**

None of the four posts could be matched confidently to a paper, dataset, code release, or conference contribution. Their example.org links are placeholders, so the numerical results should be treated as unverified claims. Related primary sources include the [ElectroFace interface dataset](https://www.nature.com/articles/s41597-025-05338-5), a [benchmark of short-range potentials for charged gold–water interfaces](https://arxiv.org/abs/2602.22931), a [study of MACE fine-tuning strategies](https://arxiv.org/abs/2606.12704), and the [Allegro scaling paper](https://aiichironakano.github.io/cs596/Musaelian-Allegro-v2-SC23.pdf). A [paper on oxygen defects and humid MoS₂ friction](https://onlinelibrary.wiley.com/doi/10.1002/smll.74754) is also relevant, but it does not match the claimed cryogenic machine-learning study. These sources provide context, not verification of the posts.

## 1. Claimed LoRA fine-tuning of MACE for molten LiCl–KCl
*Unknown; posted by Dana Example* · Unknown; described as a preprint · confidence: low  
<>

**What.** The post claims that a MACE model adapted using 4,000 DFT frames reproduces an experimental structure factor and reduces chloride–chloride pair-distribution error by 40% relative to the base model. It also claims a training cost of 1% of training from scratch and a code and data release.

**How.** The post says the authors fine-tuned a universal MACE model using low-rank adapters. It does not identify the starting checkpoint, adapter configuration, reference calculations, experimental data, error metric, or cost accounting.

**Why it matters.** If confirmed, the result would offer a directly relevant test of whether a pretrained potential can be adapted economically to a molten-salt liquid. The reported pair-distribution improvement and structure factor cannot be assessed without the underlying comparison.

**For your work.** The claimed LiCl–KCl application is directly relevant to molten-salt MLIP development, but the reported numbers should not guide model selection until the preprint and data are identified.

**Limitations.** No matching primary source or code and data release was identified. The comparison excludes no possible confounders because the post does not specify the error definition, training budget, or validation protocol.

## 2. Claimed dataset of 50 million DFT calculations on solvated electrode surfaces
*Unknown; posted by Open Materials Lab* · Unknown · confidence: low  
<>

**What.** The post claims an open dataset of 50 million DFT calculations on electrochemical interfaces and a benchmark for universal interatomic potentials on solvated electrode surfaces.

**How.** The post does not describe the sampled electrodes or electrolytes, DFT settings, labels, benchmark splits, or evaluated models. The meaning of one calculation in the reported total is also unspecified.

**Why it matters.** A well-documented interface dataset and benchmark could support training and evaluation of models for electrochemical systems. The claimed scale and coverage remain unverified.

**For your work.** Such a resource would be relevant to DFT and molecular-dynamics studies of electrochemical interfaces, provided its chemistry and charge conditions match the intended application.

**Limitations.** No matching primary source was identified, and the supplied URL is a placeholder. Dataset access, licensing, quality, and benchmark results cannot be checked.

## 3. Claimed oxygen-substitution effect on low-temperature MoS₂ friction
*Unknown; posted by Jordan Mock* · Unknown; described as an accepted paper · confidence: low  
<>

**What.** The post claims that reactive molecular dynamics with a machine-learning potential shows increased low-temperature friction from oxygen substitution in MoS₂. It says the result was validated against tribometer measurements from 150 to 300 kelvin.

**How.** The post provides no model architecture, training data, defect concentration, sliding setup, friction values, or quantitative comparison with experiment. Its proposed atomistic mechanism is not described.

**Why it matters.** A validated connection between chemical defects and temperature-dependent friction would be useful for interpreting MoS₂ lubricant performance. The claimed mechanism and validation cannot be assessed from the post alone.

**For your work.** The topic directly intersects the listener’s interests in MoS₂, tribology, and machine-learning molecular dynamics.

**Limitations.** No matching primary source was identified. The related paper on oxygen defects in humid MoS₂ uses ab initio molecular dynamics and addresses water-dependent friction; it should not be treated as the claimed cryogenic ML-potential study.

## 4. Claimed equivariant-potential scaling to 10,000 GPUs
*Unknown; posted by Lee Placeholder* · Unknown; described as an SC paper · confidence: low  
<>

**What.** The post claims simulations of 100 million atoms on 10,000 GPUs and a 3.1-fold throughput improvement from overlapping neighbor-list construction with message passing.

**How.** The post identifies overlap between neighbor-list construction and message passing as the optimization. It does not identify the potential architecture, machine, GPU type, comparison baseline, workload, or scaling protocol.

**Why it matters.** If verified, the result would be relevant to the practical cost of large atomistic simulations. The reported speedup cannot be interpreted without knowing what is included in the baseline and timing.

**For your work.** The claim concerns HPC scaling of MLIPs, although its usefulness for a particular molecular-dynamics workload remains unknown.

**Limitations.** No matching SC paper was identified. Prior Allegro work demonstrates large-scale equivariant-potential simulations, but it does not establish this post’s 10,000-GPU or 3.1-fold claims.

---
Selected from the feed:

- 50M DFT calculations on solvated electrode surfaces (Open dataset plus a universal-interatomic-potential benchmark for electrochemical interfaces.)
- Oxygen substitution in MoS2 raises friction at low temperature (Reactive MD with an ML potential validated against cryogenic tribometer data from 150 K to 300 K.)
- LoRA fine-tuning of MACE on molten LiCl-KCl (4,000 DFT frames reproduce the experimental structure factor and cut Cl-Cl pair-distribution error 40% at 1% of training cost.)
- Equivariant GNN potentials on 10,000 GPUs (SC paper reporting 3.1x throughput from overlapping neighbor-list construction on 100-million-atom systems.)
