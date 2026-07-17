"""AgentCore Studio App — composition root. Owner: mentor.

Imports every quadrant package (contracts/kb/engine/workbench/evalhub); wires FastAPI, boot
(`ensure_all_schemas` via get_admin_pool), and cross-schema GRANTs (Decision #3/#4). P1 stub:
package structure + import-smoke only. Real composition lands in Phase 3 (Core Composition) and
Phase 4 (Shared Runtime).
"""
