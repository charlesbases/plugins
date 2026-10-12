-- 无真实独立进程验收许可时 CHECK 拒绝写入；不注册函数同样拒绝。
PRAGMA foreign_keys = ON;
CREATE TABLE trades (
 account_id TEXT NOT NULL, trade_id TEXT NOT NULL, trade_json TEXT NOT NULL,
 PRIMARY KEY (account_id,trade_id),
 CHECK (COALESCE(investment_trade_v1(account_id,trade_id,trade_json),0)=1)
) STRICT;
CREATE INDEX trades_instrument ON trades(account_id,json_extract(trade_json,'$.instrument_id'));
CREATE TABLE ledger_meta (
 account_id TEXT PRIMARY KEY NOT NULL, meta_json TEXT NOT NULL,
 CHECK (COALESCE(investment_meta_v1(account_id,meta_json),0)=1)
) STRICT;
CREATE TRIGGER trades_no_delete BEFORE DELETE ON trades BEGIN
 SELECT RAISE(ABORT,'trade_fact_deletion_forbidden');
END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON ledger_meta BEGIN
 SELECT RAISE(ABORT,'ledger_deletion_forbidden');
END;
