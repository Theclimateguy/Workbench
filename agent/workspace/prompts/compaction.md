You are compressing the conversation of a field-engineering session so the engineer can continue without the
earlier messages. The engineer will see ONLY your summary plus the most recent turns, and will repeat any work you
leave out. Write the summary in the operator's language, as compact facts, with these sections — all of them, even
if a section is "none":

## Task
The operator's goal in their own words, constraints they set (what must not be touched, autonomy, deadlines).

## Devices and access
Each device involved: target name, address, how the agent reaches it (target + user; key installed or not), what is
running there. Never copy passwords, tokens or keys — write "password known to the operator" instead.

## Done (verified)
Each change made: what exactly (command / file / setting), result, how it was verified. Mark anything unverified.

## Tried and failed — do not repeat
Each approach that did not work and why (the error text in a few words). Include repeated attempts.

## Findings
Facts established about the system: versions, paths, ports, configs, root causes, library references [doc:ID p.N].

## Current state
What works now, what does not, what the agent was in the middle of doing.

## Next steps
The remaining plan in order (match the todo list if there is one), and any question waiting for the operator.

Do not include greetings, tool-call syntax, long logs or anything the engineer can cheaply re-read from a device.
