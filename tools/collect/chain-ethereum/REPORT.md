# How the Ethereum holder list was built and checked

Written with the files of `sources/chain-ethereum/` on 2026-11-25; sha256 values are of the files as delivered. The same checks are in `scan.json`; `check.py` repeats what can be checked offline.

## Files

| file | rows | bytes | sha256 |
|---|---|---|---|
| sources/chain-ethereum/holders.json | 4840 | 352994 | af6eb561b70b77028e651fd4f598a947f2bdafac40893f09446454b28cfcebb3 |
| sources/chain-ethereum/payments.json | 7399 | 1613962 | 7742994a3199b2ab9258a0e89a6a64ff2ebbbd0f8e6da2c4005dc77f757c2a2e |
| sources/chain-ethereum/scan.json | 1496 | 180129 | 158bb3a458b639eae9b99e5402fab47f89e49bfeefecbfa925ad71a0fe57af40 |
| tools/collect/chain-ethereum/collect.py | - | 25359 | bc46041ba55bcf01f1385337261a77d75cf93495a18c3dd47fb5a01555645ea2 |
| tools/collect/chain-ethereum/check.py | - | 7803 | 6e0d3512d172d5883938b80a3ac523e3b5139b23a70c35bbbeaea0ca4ec5d104 |

## Figures

- firstBlock: 23501863
- snapshotBlock: 26491756
- totalLogs: 349832
- chunkCount: 1496
- addressesSeen: 15959
- holderCount: 4840
- contractHolders: 1895
- mints: 7218
- mintedRaw: 7029842583603000000000000
- burns: 5736
- burnedRaw: 2819023057832000000000000
- supplyRaw: 4210819525771000000000000
- sumRaw: 4210819525771000000000000
- paymentRows: 7399
- holdersDigest: b7821f527799170ea380616debdc2a18ced45554acfd8a920ef458f08224a321
- logsDigest: a75d601aad139108cdc27668886af906e381d91f05f692f319ca6f0e3dcdc900
- eip7702Delegated: 1514

## Commands

- `collect.py --rpc <relay> --cache A --workers 6 (full scan, run 1, to test/scratch)`: exit 0; logs 349832 seen 15959 holders 4840 digest b7821f52...; calls 6454 checks 7 failed 0
- `collect.py --rpc <relay> --cache B --workers 6 --cross-rpc publicnode, tenderly, drpc (full scan again, run 2)`: same logs, holders, holdersDigest, logsDigest, chunk counts and payment rows as run 1; calls 7054 checks 25 failed 0 not run 11
- `collect.py, run 2's logs resumed from cache, code and state read again (run 3, wrote the delivered files)`: exit 0; holders.json rows 4840, payments.json rows 7399, scan.json rows 1496; calls 5097 checks 25 failed 0 not run 11
- `python3 tools/collect/chain-ethereum/check.py`: 33 ok lines, all checks passed, exit 0
- `check.py on 7 damaged copies (balance changed, duplicate holder, payment removed, amount changed, chunk count changed, digest changed, time at T)`: exit 1 on each
- `Etherscan API V2 getcontractcreation of the token`: blockNumber 23501863, same as the bisect

## Checks

- passed: snapshot block hash and time equal snapshot.json (0xcf579ca7207cbf6760375d4d82669e9caee0dc3c2182f64b5b7aaca0f1b57dc4 2026-11-25T10:59:59Z)
- passed: no negative balance (0 negative of 15959 addresses seen)
- passed: balances sum to totalSupply() at the block (sum 4210819525771000000000000, supply 4210819525771000000000000)
- passed: mints less burns equal totalSupply() at the block (7029842583603000000000000 less 2819023057832000000000000 is 4210819525771000000000000)
- passed: balanceOf at the block equals ours for 25 holders (25 of 25 equal)
- passed: balanceOf at the block equals ours for every address seen (15959 of 15959 addresses seen equal (zero balances too), through Multicall3 0xca11bde05977b3631167028862be2a173976ca11)
- passed: payment blockTime equals eth_getBlockByNumber (25 of 25 sampled payment blocks equal)
- passed: https://ethereum-rpc.publicnode.com: snapshot block hash (0xcf579ca7207cbf6760375d4d82669e9caee0dc3c2182f64b5b7aaca0f1b57dc4)
- not run: https://ethereum-rpc.publicnode.com: first block has code, the one before none (HTTP 403 {"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: <url>)
- not run: https://ethereum-rpc.publicnode.com: totalSupply() at the block equals the sum (HTTP 403 {"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: <url>)
- not run: https://ethereum-rpc.publicnode.com: balanceOf at the block equals ours for 25 holders (HTTP 403 {"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: <url>)
- not run: https://ethereum-rpc.publicnode.com: balanceOf at the block equals ours for every address seen (HTTP 403 {"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: <url>)
- not run: https://ethereum-rpc.publicnode.com: sampled log windows equal ours (HTTP 403 {"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: <url>)
- not run: https://gateway.tenderly.co/public/mainnet: snapshot block hash (HTTP 429 Rate limited)
- not run: https://gateway.tenderly.co/public/mainnet: first block has code, the one before none (HTTP 429 Rate limited)
- not run: https://gateway.tenderly.co/public/mainnet: totalSupply() at the block equals the sum (HTTP 429 Rate limited)
- not run: https://gateway.tenderly.co/public/mainnet: balanceOf at the block equals ours for 25 holders (HTTP 429 Rate limited)
- not run: https://gateway.tenderly.co/public/mainnet: balanceOf at the block equals ours for every address seen (HTTP 429 Rate limited)
- not run: https://gateway.tenderly.co/public/mainnet: sampled log windows equal ours (HTTP 429 Rate limited)
- passed: https://eth.drpc.org: snapshot block hash (0xcf579ca7207cbf6760375d4d82669e9caee0dc3c2182f64b5b7aaca0f1b57dc4)
- passed: https://eth.drpc.org: first block has code, the one before none (blocks 23501863 and 23501862)
- passed: https://eth.drpc.org: totalSupply() at the block equals the sum (4210819525771000000000000)
- passed: https://eth.drpc.org: balanceOf at the block equals ours for 25 holders (25 of 25 equal)
- passed: https://eth.drpc.org: balanceOf at the block equals ours for every address seen (15959 of 15959 addresses seen equal (zero balances too), through Multicall3 0xca11bde05977b3631167028862be2a173976ca11)
- passed: https://eth.drpc.org: sampled log windows equal ours (40 of 40 served windows of 100 blocks equal (3111 logs), 0 windows not served)

## Missing or uncertain

- After run 3, the endpoint entry in collect.py gained chunksReadByAnEarlierRun (1496) and the same field was written into scan.json by a small script rather than by a fourth run, to stay inside the 20000-call limit. check.py was run after that.
- The relay's address is not published anywhere, as the task requires; scan.json endpoints says what each public endpoint served.
- collect.py with a cold cache needs an archive endpoint that serves 2000-block log ranges, or it halves; with drpc alone that is about 30000 calls (not tried).
- Wrote the Ethereum holder list from 349832 Transfer logs, blocks 23501863-26491756: 4840 holders of 15959 addresses seen, supply 4210819.525771 IMD, 7399 payment rows. All required checks passed. For the next pieces: (1) isContract is eth_getCode non-empty as briefed, so 1514 of the 1895 true rows are EIP-7702 delegated accounts, i.e. ordinary wallets; scan.json eip7702Delegated lists them [wallet, delegate]. Treat only the other 381 as contracts. (2) No Transfer ever touched intake 0xa43e6f75 or legacy intake 0x1397434c: the first sampled intake transactions (Etherscan txlist) moved IMD straight from the payer to the payment address, so `from` is the payer there too and the rows do not say which route was used. (3) payTo: 7384 in from 684 wallets (420 hold IMD at the block), 15 out (sweeps). First payment 2026-09-23T06:32:35Z, last 2026-11-25T10:45:59Z. A row is a transfer, not proof of an order: 7359 of 7399 rows are 0.5 IMD, but not all need be orders. (4) Balances of the sIMD vault 0x9efa934d (46% of supply) and the Uniswap v4 pool manager 0x000000000004444c5dc75cb358380d2e3de08a90 are listed as those contracts', not looked through to depositors. Uncertain: the full log read went through this machine's chain 1 relay. No public endpoint served it whole: publicnode refuses archive reads (403), tenderly answered 429 to every call, drpc allows 100 blocks a call. drpc did repeat block hash, first block, totalSupply, all 15959 balances and 40 random 100-block windows (3111 logs), all equal. The chunks came from the second full scan (12:16:01Z-12:16:56Z); the run that wrote the files read them from its cache. No read was left failed (some batches were retried).
