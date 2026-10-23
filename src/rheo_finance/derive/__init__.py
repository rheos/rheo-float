"""The derivation: ``derive.run.run`` truncates the derived tables and rebuilds them in
one transaction, stage by stage (SPEC "Derivation").

``stage1`` parses every registered document, ``stage2`` loads the manual files,
``stage3`` turns lines into txns: ``precedence`` ranks documents per account and
``overlap`` decides whether each line is a txn, a source of one, or a discrepancy.
Their data dependency, not their numbers, sets the order ``run`` calls them in:
parsers match accounts from the account table, so the manual files load first.
"""
