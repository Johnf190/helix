# How Helix remembers (starter fact, {TODAY})
Category: helix

Helix keeps durable knowledge as one fact per file in `memory/facts/`. Every answer loads the charter
(`memory/BRAIN.md`), the pinned facts, and the facts most relevant to the current question.

- To save something: ask Odin to remember it. He writes a new numbered fact file.
- To correct something: the new fact says which fact it supersedes; the old one keeps a pointer to the new.
- To see everything: open the Memory view in the interface, or the folder itself.

This is an example of what a fact looks like. Edit it or replace it.
