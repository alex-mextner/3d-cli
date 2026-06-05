# Sources - annotated link list

Every tool, library, and paper cited in `report.md`, with a one-line note. Grouped
by report section. URLs are full and visible so they survive PDF conversion.

## Papers & methods (Sections 3, 5)

- **FlipFlop effect** - LLMs flip ~46% of answers and lose ~17% accuracy when challenged ("are you sure?"); the core reason a critique loop must not be self-judged. https://arxiv.org/abs/2311.08596
- **ReLook** - vision-grounded RL for agentic web coding; introduces forced-monotonic acceptance (accept only improving revisions) + zero-reward for invalid renders. The blueprint for the convergence loop. https://arxiv.org/abs/2510.11498
- **ReLook (HF paper page)** - mirror with abstract/discussion. https://huggingface.co/papers/2510.11498
- **Single-View 3D Reconstruction via Differentiable Rendering and Inverse Procedural Modeling** - canonical academic instance of fitting procedural params to one image via silhouette-mode gradients. https://www.mdpi.com/2073-8994/16/2/184
- **A Simple Approach to Differentiable Rendering of SDFs** - route to gradient-based silhouette fitting on signed-distance fields. https://arxiv.org/html/2405.08733v1
- **COLMAP-free 3D Gaussian Splatting** - novel-view 3DGS without precomputed camera poses. https://arxiv.org/abs/2312.07504
- **Depth Anything V2 (paper)** - NeurIPS 2024 monocular depth; synthetic+pseudo-labeled training; big zero-shot gain over MiDaS. https://arxiv.org/html/2406.09414v1
- **Does Point Cloud Boost Spatial Reasoning of LLMs?** - empirical study of 3D-signal fusion into LLMs. https://arxiv.org/pdf/2504.04540
- **Survey of Spatial Reasoning in LLM** - taxonomy of direct/step-by-step/task-specific 3D-to-LLM alignment. https://arxiv.org/pdf/2504.05786
- **3D Gaussian Splatting explainer** - practical intro + NeRFStudio training. https://learnopencv.com/3d-gaussian-splatting/
- **Differentiable renderer comparison figure** - SoftRas vs PyTorch3D vs Mitsuba 2 vs nvdiffrast. https://www.researchgate.net/figure/Comparison-with-SoftRas-LLCL19-PyTorch3D-RRN20-Mitsuba-2-NDVZJ19-and-Nvdiffrast_fig3_353262797

## Authoring tools (Section 1)

- **OpenSCAD** - scripted CSG CAD; the project anchor. https://openscad.org/
- **OpenSCAD CLI manual (official)** - headless flags: -o, --render, --camera, --projection, --imgsize. https://files.openscad.org/documentation/manual/Using_OpenSCAD_in_a_command_line_environment.html
- **OpenSCAD CLI (wikibooks)** - same, with examples. https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Using_OpenSCAD_in_a_command_line_environment
- **openscad#840** - camera viewpoints differ between --render and preview; lock the camera. https://github.com/openscad/openscad/issues/840
- **CadQuery** - fluent Python CAD on OpenCASCADE; rapid prototyping with real fillets. https://github.com/CadQuery/cadquery
- **CadQuery intro (Adafruit, 2026)** - current overview. https://blog.adafruit.com/2026/04/21/cadquery-a-python-module-for-building-parametric-3d-cad-models/
- **build123d** - CadQuery's successor; context-manager + algebra API; cleaner Python. https://github.com/gumyr/build123d
- **build123d docs** - introduction and API. https://build123d.readthedocs.io/
- **build123d vs CadQuery comparison** - production-control vs rapid-prototyping framing. https://build123d.readthedocs.io/en/latest/introduction.html
- **SolidPython2** - generate OpenSCAD from Python; keeps the OpenSCAD ecosystem. https://github.com/jeff-dh/SolidPython
- **Blender** - headless mesh/render powerhouse via bpy. https://www.blender.org/
- **Mastering the Blender CLI** - --background/--python/--python-expression usage. https://renderday.com/blog/mastering-the-blender-cli
- **blender-cli-rendering** - reference bpy render scripts. https://github.com/yuki-koyama/blender-cli-rendering
- **ImplicitCAD** - Haskell SDF-based CAD with OpenSCAD-like syntax; free rounding. https://github.com/Haskell-Things/ImplicitCAD
- **libfive** - F-rep/SDF modeling kernel; smooth blends. https://libfive.com/
- **Zoo Text-to-CAD** - commercial text->B-rep CAD (ML-ephant + KittyCAD API). https://zoo.dev/text-to-cad
- **Zoo ML API** - the ML-ephant developer API. https://zoo.dev/machine-learning-api
- **Introducing Text-to-CAD (Zoo blog)** - design rationale + Zookeeper agent modes. https://zoo.dev/blog/introducing-text-to-cad
- **Zoo open-source coverage** - 3DPrintingIndustry writeup. https://3dprintingindustry.com/news/open-source-ai-text-to-cad-software-by-zoo-unlocks-accessible-3d-design-236964/

## OpenSCAD libraries (Section 2)

- **OpenSCAD libraries index** - the canonical list. https://openscad.org/libraries.html
- **BOSL2** - the must-have: rounded primitives, attachments, sweeps, threads, gears. https://github.com/BelfrySCAD/BOSL2
- **BOSL2 threading.scad** - ISO/trapezoidal/ACME/bottle threads. https://github.com/BelfrySCAD/BOSL2/wiki/threading.scad
- **dotSCAD** - Bezier/spline/polar math, morphing. https://github.com/JustinSDK/dotSCAD
- **NopSCADlib** - real vitamins (Raspberry Pi, fans, screws) + BOM/assembly. https://github.com/nophead/NopSCADlib
- **Round-Anything** - polyRound 2D fillets + extrudeWithRadius. https://github.com/Irev-Dev/Round-Anything
- **MCAD** - bundled library: gears, bearings, shapes. https://github.com/openscad/MCAD
- **Relativity.scad** - CSS-like relative positioning DSL. https://github.com/davidson16807/relativity.scad
- **threads-scad (rcolyer)** - standalone lightweight ISO threads. https://github.com/rcolyer/threads-scad
- **Parametric Involute Bevel/Spur Gears (GregFrost)** - canonical standalone gears. https://www.thingiverse.com/thing:3575
- **cfinke/LEGO.scad** - polished parametric LEGO brick/tile/plate generator (block()). https://github.com/cfinke/LEGO.scad
- **cfinke/Technic.scad** - Technic pins/holes generator. https://github.com/cfinke/Technic.scad
- **richfelker/brickify** - brick from an arbitrary 2D outline; ideal for non-rectangular base plate. https://github.com/richfelker/brickify
- **anandamous/OpenSCADLEGO** - parametric brick with exposed LEGO constants. https://github.com/anandamous/OpenSCADLEGO
- **mlkood/BRICK.scad** - LEGO brick generator fork. https://github.com/mlkood/BRICK.scad

## Mesh kernels & QA (Sections 1, 4)

- **manifold** - guaranteed-manifold boolean kernel; OpenSCAD's fast-CSG engine. https://github.com/elalish/manifold
- **trimesh** - Python mesh Swiss-army (watertight/volume/raycast/SDF/boolean). https://github.com/mikedh/trimesh
- **Open3D** - meshes+point clouds; is_edge/vertex_manifold, is_watertight; depth bridge. https://www.open3d.org/
- **Open3D mesh tutorial** - watertight = edge+vertex manifold + not self-intersecting. https://www.open3d.org/docs/release/tutorial/geometry/mesh.html
- **PyVista** - VTK plotting + off-screen screenshots for QA. https://pyvista.org/
- **PyMeshLab** - MeshLab filters in Python: remesh, Hausdorff, Poisson. https://github.com/cnr-isti-vclab/PyMeshLab
- **ADMesh** - tiny STL diagnostic/repair CLI. https://github.com/admesh/admesh
- **F3D** - fast scriptable mesh viewer with headless PNG output. https://f3d.app/
- **Gmsh** - scriptable FEA mesh generator. https://gmsh.info/
- **ImageMagick compare** - AE/RMSE/SSIM/DSSIM/PHASH metrics + -fuzz. https://imagemagick.org/script/compare.php
- **lib3mf** - reference 3MF read/write/validate library. https://github.com/3MFConsortium/lib3mf
- **BambuStudio#3316** - Bambu 3MF extensions not always portable to other slicers. https://github.com/bambulab/BambuStudio/issues/3316
- **Netfabb free guide** - pro mesh repair + wall-thickness analysis. https://3dprintingindustry.com/free-guide-to-autodesk-netfabb/
- **Pre-print check tool comparison (3ders)** - Netfabb/MeshLab/etc. compared. https://www.3ders.org/articles/20140715-comparison-test-of-four-pre-print-check-tools.html
- **Best mesh repair (Tripo)** - 2025 repair-tool roundup. https://www.tripo3d.ai/content/en/use-case/the-best-mesh-repair-for-print
- **CalculiX** - free open-source FEM (static/nonlinear/thermal/contact). https://www.calculix.de/
- **Free FEA overview (caeflow)** - CalculiX/Elmer capabilities. https://caeflow.com/fea/free-fea-program/
- **Elmer FEM** - multiphysics, thermal-structural, HPC. https://www.elmerfem.org/
- **OpenAM-SimCCX** - CalculiX 2.21 in an open-source AM thermo-mechanical workflow. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12608665/

## Slicers & MCP (Sections 1, 3)

- **Bambu Studio CLI (wiki)** - headless --slice/--load-settings/--export-3mf. https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
- **Bambu Studio CLI reference (Printago)** - flag-by-flag headless slicing guide. https://printago.io/blog/bambu-studio-cli-reference
- **CuraEngine** - headless slicing core of Cura. https://github.com/Ultimaker/CuraEngine
- **jhacksman/OpenSCAD-MCP-Server** - text->model MCP with multi-view recon + OpenSCAD. https://github.com/jhacksman/OpenSCAD-MCP-Server
- **quellant/openscad-mcp** - OpenSCAD render/export MCP. https://github.com/quellant/openscad-mcp
- **fboldo/openscad-mcp-server** - STL+PNG render MCP. https://github.com/fboldo/openscad-mcp-server

## AI 3D & perception models (Section 6)

- **Depth Anything V2 (repo)** - SOTA single-image relative depth; mask the reference, get depth ordering. https://github.com/DepthAnything/Depth-Anything-V2
- **SpatialLM** - LLM for structured indoor modeling from point clouds. https://manycore-research.github.io/SpatialLM/
- **TripoSR** - sub-second feed-forward image->mesh (low fidelity). https://github.com/VAST-AI-Research/TripoSR
- **Stable Fast 3D** - real-time single-image image->mesh with UV/material. https://github.com/Stability-AI/stable-fast-3d
- **Point-E** - OpenAI point-cloud diffusion (coarse). https://github.com/openai/point-e
- **Shap-E** - OpenAI implicit (NeRF/SDF) diffusion -> mesh via marching cubes. https://github.com/openai/shap-e
- **TRELLIS.2 guide (Apatero)** - current image-to-3D quality leader; PBR materials. https://www.apatero.com/blog/trellis-2-comfyui-image-to-3d-complete-guide-2025
- **Trellis/Hunyuan3D comparison (3DAI Studio)** - quality/topology tradeoffs. https://www.3daistudio.com/blog/pixal3d-vs-trellis-2-vs-hunyuan-3d-comparison
- **Open-source 3D-gen APIs (pixazo)** - multi-view diffusion + recon is the 2026 pattern. https://www.pixazo.ai/blog/best-open-source-3d-model-generation-apis
- **COLMAP** - de-facto SfM+MVS photogrammetry pipeline (multi-view). https://colmap.github.io/
- **Meshroom / AliceVision** - GUI photogrammetry alternative. https://alicevision.org/
- **Mitsuba 3** - retargetable forward+inverse differentiable renderer (Dr.Jit). https://www.mitsuba-renderer.org/
- **Mitsuba 3 repo** - source + Dr.Jit. https://github.com/mitsuba-renderer/mitsuba3
- **Mitsuba 3 inverse-rendering tutorials** - backprop image loss into scene. https://mitsuba.readthedocs.io/en/stable/src/inverse_rendering_tutorials.html
- **nvdiffrast** - NVIDIA differentiable rasterization with analytic visibility gradients. https://github.com/NVlabs/nvdiffrast

## Program synthesis for CAD (Section 19, added)

- **CSGNet** - CNN+RNN that parses a 2D/3D shape into a CSG program (primitives + booleans); trained with policy-gradient RL on the render-vs-input visual difference. The seminal "image -> CSG program" model; CVPR 2018. https://arxiv.org/abs/1712.08290
- **CSGNet (PAMI extended)** - longer journal version with the memory-augmented encoder and more experiments. https://arxiv.org/abs/1912.11393
- **ShapeAssembly** - DSL + hierarchical sequence-VAE that writes programs declaring cuboid part proxies attached to one another, parameterised with continuous free variables (a program = a shape family). SIGGRAPH Asia 2020. https://arxiv.org/abs/2009.08026
- **DeepCAD** - Transformer autoencoder modelling a CAD solid as a sequence of CAD operations (sketch/extrude/boolean); "CAD as a token sequence"; 178k-model dataset. ICCV 2021. https://arxiv.org/abs/2105.09492

## Single-image-to-3D lineage (Section 20, added)

- **LRM: Large Reconstruction Model** - 500M-param transformer regressing a triplane NeRF from one image in ~5 s; trained on ~1M objects; root of the feed-forward single-image-to-3D wave. ICLR 2024. https://arxiv.org/abs/2311.04400
- **InstantMesh** - multi-view diffusion + sparse-view LRM + FlexiCubes; clean mesh from one image in ~10 s; the dominant 2-stage pattern. https://arxiv.org/abs/2404.07191
- **Wonder3D** - cross-domain diffusion generating consistent multi-view NORMAL maps + color, fused to a textured mesh; the normal maps are the useful scaffolding signal here. https://arxiv.org/abs/2310.15008
- **Wonder3D++** - higher-fidelity successor (cross-domain diffusion). https://arxiv.org/abs/2511.01767

## Depth & segmentation foundation models (Sections 6, 20, added)

- **Marigold** - repurposes Stable Diffusion for affine-invariant monocular depth; fine-tuned on synthetic data, zero-shot transfer; diffusion route with superior edge fidelity. CVPR 2024 (Oral, Best Paper candidate). https://arxiv.org/abs/2312.02145
- **SAM 2** - promptable visual segmentation in images AND video; streaming-memory transformer; 6x faster and more accurate than SAM 1 on images. The reference-silhouette front-end. https://arxiv.org/abs/2408.00714

## Code-CAD / OpenSCAD-LLM benchmarks (Section 21, added)

- **ModelRift OpenSCAD-LLM benchmark** - the only OpenSCAD-specific benchmark: image (Pantheon photos) -> .scad with CLI-render iteration, scored on a SUBJECTIVE 0-5 quality scale (its flaw). https://modelrift.com/blog/openscad-llm-benchmark
- **BlenderLLM + CADBench** - LLM fine-tuned to generate Blender CAD scripts via iterative self-improvement; introduces the CADBench evaluation suite (automated metrics, not subjective). The methodology template for `3d ai bench`. https://arxiv.org/abs/2412.14203

## Evaluation metrics - precise definitions (Section 22, added)

- **Tatarchenko et al., "What Do Single-View 3D Reconstruction Networks Learn?"** - argues IoU and Chamfer can mislead; recommends F-score@tau (harmonic mean of precision/recall at distance threshold) as the primary surface metric. CVPR 2019. https://arxiv.org/abs/1905.03678
- **SSIM (Wang et al. 2004)** - structural similarity: luminance x contrast x structure; SSIM = [(2 mu_x mu_y + C1)(2 sigma_xy + C2)]/[(mu_x^2+mu_y^2+C1)(sigma_x^2+sigma_y^2+C2)], C1=(0.01 L)^2, C2=(0.03 L)^2. IEEE TIP 13(4). https://www.cns.nyu.edu/pub/eero/wang03-reprint.pdf
- **SSIM (Wikipedia)** - exact formula + variable meanings, cross-check. https://en.wikipedia.org/wiki/Structural_similarity_index_measure
- **LPIPS (Zhang et al. 2018)** - "The Unreasonable Effectiveness of Deep Features as a Perceptual Metric": weighted MSE over channel-normalised deep features of a pretrained net; correlates with human judgement far better than PSNR/SSIM. CVPR 2018. https://arxiv.org/abs/1801.03924
- **CLIPScore (Hessel et al. 2021)** - reference-free metric: CLIPScore(I,C) = max(100 * cos(E_I, E_C), 0); image-image variant for semantic "is it the right object." EMNLP 2021. https://arxiv.org/abs/2104.08718

## FDM strength & layer-adhesion anisotropy - peer reviewed (Section 23, added)

- **Ahn et al. 2002, "Anisotropic material properties of FDM ABS," Rapid Prototyping Journal 8(4):248-257** - the foundational FDM-anisotropy paper: raster/build orientation is the dominant strength factor; load across layers pulls the weak weld interface; FDM reaches only a fraction of injection-moulded strength. https://www.emerald.com/insight/content/doi/10.1108/13552540210441166/full/html
- **"Influence of Processing Parameters on the Specific Tensile Strength of FDM PET-G and PLA" (2022, PMC9230522)** - 114 specimens; UTS by orientation: PET-G XY 19.27 / XZ 14.30 / YZ 16.48 MPa (XY +34.8% over weakest); PLA XY 21.70 / XZ 9.55 / YZ 15.45 MPa (XY +127%); XZ always weakest, PLA far more anisotropic than PET-G. https://pmc.ncbi.nlm.nih.gov/articles/PMC9230522/
- **"Impact of Layer Height and Annealing on Tensile Strength and Dimensional Accuracy of FDM Parts" (MDPI Materials 16(13):4574)** - layer height dominates annealing for interlayer adhesion / strength (PLA, PETG, CF-PETG). https://www.mdpi.com/1996-1944/16/13/4574
