"""Public key that PC-app updates must be signed with (the private half is the UPDATE_SIGNING_KEY GitHub secret).

An update downloaded from the server is only installed if its Ed25519 signature checks out against this key, so
nobody who merely has the API key or access to the server can make the machine PC run their own program.
"""
PUBLIC_KEY_HEX = "3f473fcf53564671f7ea333996abd0711f48831deadf95b9255be278737ddd4d"
