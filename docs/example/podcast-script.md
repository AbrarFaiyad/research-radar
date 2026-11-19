**A:** Four materials-science claims lack identifiable primary sources. That is today’s headline. Each post describes something directly relevant to computational materials research, but none gives us a working route to the underlying paper, data, or code. We searched for the described methods and results. We found related research, but not a reliable match for any of the four claims.

**B:** So this is a briefing about what the posts claim, and about the checks we would need before using those claims. A similar paper is not enough to establish that a particular post is real.

**A:** Exactly. Let’s start with the closest fit to the listener’s molten-salt work. One post claims a universal MACE model was fine-tuned on four thousand density functional theory frames of molten lithium chloride–potassium chloride. The poster says the adapted model reproduces an experimental structure factor and reduces the error in the chloride–chloride pair distribution by forty percent relative to the base model.

**B:** And there is a second comparison. The post claims the training cost was one percent of training a model from scratch. That sounds appealing, but we do not know what was counted as training cost. We also do not know whether the scratch model received the same data, how it was tuned, or whether DFT data generation was included.

**A:** The proposed adaptation uses low-rank adapters, often called LoRA. In general, the idea is to constrain weight updates to a lower-dimensional form while retaining the pretrained model as the starting point. The post does not tell us which MACE checkpoint was adapted, which layers received adapters, or what rank was used.

**B:** There is a real research context here. A study of MACE fine-tuning strategies compares full-parameter updates, LoRA, and replay approaches across several benchmarks. That study is useful background, but we cannot identify it as the claimed molten-salt preprint. For this post, we still need the release itself.

**A:** What would you check first if the claimed data appeared?

**B:** I would check how the four thousand frames were selected and how independent the test configurations are. Then I would ask how the chloride–chloride error was defined. A forty percent reduction can mean different things depending on the distance range and the error metric. I would also compare the structure factor and pair distributions across the relevant temperatures and compositions, and check whether the simulation remains stable.

**A:** Next is the claimed electrochemical-interface dataset. The post announces fifty million DFT calculations on solvated electrode surfaces and a benchmark for universal interatomic potentials. If a resource of that scope exists, it could be highly relevant to training and testing models at solid–liquid interfaces.

**B:** But the number alone is hard to interpret. Does a calculation mean a single configuration, a trajectory, or something else? Which metals, electrolytes, charge states, and surface structures are represented? The post gives none of those details, and we could not identify a matching dataset.

**A:** There are genuine resources in the surrounding literature. ElectroFace provides interface data for reuse. A separate benchmark studies short-range machine-learning potentials for charged gold–water interfaces containing sodium ions. Those works can help frame the problem, but neither verifies the fifty-million-calculation announcement.

**B:** For the listener’s own interface work, the key question is coverage. A large collection is useful only to the extent that its configurations and labels support the intended chemistry and conditions. A benchmark also needs a clear split and named models before we can interpret any claim about universal-potential performance.

**A:** The third post concerns molybdenum disulfide, or MoS₂, and friction. It claims that reactive molecular dynamics with a machine-learning potential shows higher low-temperature friction when oxygen substitutes into MoS₂. The poster says the simulations were validated against tribometer measurements from one hundred fifty to three hundred kelvin.

**B:** That would be a compelling combination of atomistic modeling and experiment. But the post gives no friction values, defect concentration, model details, or quantitative agreement. It also does not state the proposed mechanism. We cannot tell what the simulations actually established.

**A:** We did find a paper on oxygen defects, water, and friction in MoS₂. It uses ab initio molecular dynamics to study humid interfaces and reports a role for water trapped at oxygen defects. That is relevant context for tribology, but the methods and conditions differ from the claimed low-temperature machine-learning study.

**B:** That distinction matters. Humidity, temperature, defect chemistry, and contact conditions can all affect a friction comparison. We should not transfer the mechanism or the experimental validation from one study to another just because both discuss oxygen and MoS₂.

**A:** The final claim is about computing scale. A poster describes an SC paper that ran an equivariant graph neural network potential on ten thousand GPUs for a system of one hundred million atoms. The claimed optimization overlaps neighbor-list construction with message passing, and the post reports a three-point-one-fold throughput improvement.

**B:** The missing baseline is central. Was the speedup measured against the same model and machine without overlap? Does throughput include the full molecular-dynamics step? Is the comparison at the same number of GPUs and atoms? Without those details, the multiplier tells us very little about what another group could expect.

**A:** We found prior large-scale work on Allegro. The SC paper reports scaling studies for an equivariant potential, including a one-hundred-million-atom scaling case. Allegro uses a strictly local architecture designed to make parallel computation practical. That is established context, but it is not evidence for the new post’s claimed GPU count or optimization.

**B:** And architecture matters when comparing these results. A speedup from changing how work is scheduled depends on the implementation and workload. A reported large atom count by itself does not tell us the time per step, scaling efficiency, or cost of a scientifically useful trajectory.

**A:** The takeaway is to keep the questions and hold the numbers. The molten-salt adaptation, the interface dataset, the MoS₂ friction mechanism, and the GPU optimization all address real research needs. Today, though, none of the four specific releases has an identifiable primary source. Before saving them as results, we need titles or working links that lead to the papers, data, or code.