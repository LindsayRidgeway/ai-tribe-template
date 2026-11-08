# Who a member of this world may write to

Written by the human, not by a member. A work that carries `to:` an address that is not in this table is
listed by `python3 outbox.py` as *not offered for sending* — a member may not invent a correspondent, and
neither may a typo. Nothing in this repository sends mail: a person reads the letter, posts it from their
own mail client, and runs `python3 outbox.py --sent <file>` so the record says it went.

One row per person. Delete the rows you do not want and add your own:

| who | address | note |
|---|---|---|
