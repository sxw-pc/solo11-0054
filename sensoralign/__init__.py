"""SensorAlign: deterministic multi-sensor time-base alignment.

The package is an offline batch tool.  It loads synthetic or locally-owned
sensor exports, aligns them on an integer-microsecond grid, estimates clock
offsets, and writes auditable reports and a SQLite ledger.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"
