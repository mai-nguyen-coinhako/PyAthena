# -*- coding: utf-8 -*-
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from pyathena import OperationalError
from pyathena.converter import Converter
from pyathena.error import ProgrammingError
from pyathena.model import AthenaQueryExecution
from pyathena.result_set import AthenaResultSet
from pyathena.util import RetryConfig, parse_output_location

if TYPE_CHECKING:
    from pandas import DataFrame

    from pyathena.connection import Connection

_logger = logging.getLogger(__name__)  # type: ignore


class AthenaPandasResultSet(AthenaResultSet):

    _parse_dates: List[str] = [
        "date",
        "time",
        "time with time zone",
        "timestamp",
        "timestamp with time zone",
    ]

    def __init__(
        self,
        connection: "Connection",
        converter: Converter,
        query_execution: AthenaQueryExecution,
        arraysize: int,
        retry_config: RetryConfig,
        keep_default_na: bool = False,
        na_values: Optional[Iterable[str]] = ("",),
        quoting: int = 1,
        unload: bool = False,
        unload_location: Optional[str] = None,
        engine: str = "auto",
        **kwargs,
    ) -> None:
        super(AthenaPandasResultSet, self).__init__(
            connection=connection,
            converter=converter,
            query_execution=query_execution,
            arraysize=1,  # Fetch one row to retrieve metadata
            retry_config=retry_config,
        )
        self._rows.clear()  # Clear pre_fetch data
        self._arraysize = arraysize
        self._keep_default_na = keep_default_na
        self._na_values = na_values
        self._quoting = quoting
        self._unload = unload
        self._unload_location = unload_location
        self._engine = engine
        self._data_manifest: List[str] = []
        self._kwargs = kwargs
        self._fs = self.__s3_file_system()
        if self.state == AthenaQueryExecution.STATE_SUCCEEDED and self.output_location:
            self._df = self._as_pandas()
        else:
            import pandas as pd

            self._df = pd.DataFrame()
        self._iterrows = enumerate(self._df.to_dict("records"))

    def __get_engine(self) -> "str":
        if self._engine == "auto":
            import importlib

            error_msgs = ""
            for engine in ["pyarrow", "fastparquet"]:
                try:
                    module = importlib.import_module(engine)
                    return module.__name__
                except ImportError as e:
                    error_msgs += f"\n - {str(e)}"
            raise ImportError(
                "Unable to find a usable engine; tried using: 'pyarrow', 'fastparquet'.\n"
                "A suitable version of pyarrow or fastparquet is required for parquet support.\n"
                "Trying to import the above resulted in these errors:"
                f"{error_msgs}"
            )
        else:
            return self._engine

    def __s3_file_system(self):
        from s3fs import S3FileSystem

        return S3FileSystem(
            profile=self.connection.profile_name,
            client_kwargs={
                "region_name": self.connection.region_name,
                **self.connection._client_kwargs,
            },
        )

    @property
    def is_unload(self):
        return (
            self._unload
            and self.query
            and self.query.strip().upper().startswith("UNLOAD")
        )

    @property
    def dtypes(self) -> Dict[str, Type[Any]]:
        description = self.description if self.description else []
        return {
            d[0]: self._converter.types[d[1]]
            for d in description
            if d[1] in self._converter.types
        }

    @property
    def converters(
        self,
    ) -> Dict[Optional[Any], Callable[[Optional[str]], Optional[Any]]]:
        description = self.description if self.description else []
        return {
            d[0]: self._converter.get(d[1])
            for d in description
            if d[1] in self._converter.mappings
        }

    @property
    def parse_dates(self) -> List[Optional[Any]]:
        description = self.description if self.description else []
        return [d[0] for d in description if d[1] in self._parse_dates]

    def _trunc_date(self, df: "DataFrame") -> "DataFrame":
        description = self.description if self.description else []
        times = [d[0] for d in description if d[1] in ("time", "time with time zone")]
        if times:
            df.loc[:, times] = df.loc[:, times].apply(lambda r: r.dt.time)
        return df

    def fetchone(
        self,
    ) -> Optional[Union[Tuple[Optional[Any], ...], Dict[Any, Optional[Any]]]]:
        try:
            row = next(self._iterrows)
        except StopIteration:
            return None
        else:
            self._rownumber = row[0] + 1
            description = self.description if self.description else []
            return tuple([row[1][d[0]] for d in description])

    def fetchmany(
        self, size: Optional[int] = None
    ) -> List[Union[Tuple[Optional[Any], ...], Dict[Any, Optional[Any]]]]:
        if not size or size <= 0:
            size = self._arraysize
        rows = []
        for _ in range(size):
            row = self.fetchone()
            if row:
                rows.append(row)
            else:
                break
        return rows

    def fetchall(
        self,
    ) -> List[Union[Tuple[Optional[Any], ...], Dict[Any, Optional[Any]]]]:
        rows = []
        while True:
            row = self.fetchone()
            if row:
                rows.append(row)
            else:
                break
        return rows

    def _read_csv(self) -> "DataFrame":
        import pandas as pd

        if not self.output_location:
            raise ProgrammingError("OutputLocation is none or empty.")
        if not self.output_location.endswith((".csv", ".txt")):
            return pd.DataFrame()
        length = self._get_content_length()
        if length and self.output_location.endswith(".txt"):
            sep = "\t"
            header = None
            description = self.description if self.description else []
            names = [d[0] for d in description]
        elif length and self.output_location.endswith(".csv"):
            sep = ","
            header = 0
            names = None
        else:
            return pd.DataFrame()
        try:
            # TODO chunksize
            df = pd.read_csv(
                self.output_location,
                sep=sep,
                header=header,
                names=names,
                dtype=self.dtypes,
                converters=self.converters,
                parse_dates=self.parse_dates,
                infer_datetime_format=True,
                skip_blank_lines=False,
                keep_default_na=self._keep_default_na,
                na_values=self._na_values,
                quoting=self._quoting,
                storage_options={
                    "profile": self.connection.profile_name,
                    "client_kwargs": {
                        "region_name": self.connection.region_name,
                        **self.connection._client_kwargs,
                    },
                },
                **self._kwargs,
            )
            return self._trunc_date(df)
        except Exception as e:
            _logger.exception(f"Failed to read {self.output_location}.")
            raise OperationalError(*e.args) from e

    def _read_parquet(self, engine) -> "DataFrame":
        import pandas as pd

        self._data_manifest = self._read_data_manifest()
        if not self._data_manifest:
            return pd.DataFrame()
        if not self._unload_location:
            self._unload_location = (
                "/".join(self._data_manifest[0].split("/")[:-1]) + "/"
            )

        if engine == "pyarrow":
            unload_location = self._unload_location
            kwargs = {
                "use_threads": True,
                "use_legacy_dataset": False,
            }
        elif engine == "fastparquet":
            unload_location = f"{self._unload_location}*"
            kwargs = {}
        else:
            raise ProgrammingError("Engine must be one of `pyarrow`, `fastparquet`.")
        kwargs.update(self._kwargs)

        try:
            return pd.read_parquet(
                unload_location,
                engine=self._engine,
                storage_options={
                    "profile": self.connection.profile_name,
                    "client_kwargs": {
                        "region_name": self.connection.region_name,
                        **self.connection._client_kwargs,
                    },
                },
                use_nullable_dtypes=False,
                **kwargs,
            )
        except Exception as e:
            _logger.exception(f"Failed to read {self.output_location}.")
            raise OperationalError(*e.args) from e

    def _read_parquet_schema(self, engine) -> Tuple[Dict[str, Any], ...]:
        if engine == "pyarrow":
            from pyarrow import parquet

            from pyathena.arrow.util import to_column_info

            if not self._unload_location:
                raise ProgrammingError("UnloadLocation is none or empty.")
            bucket, key = parse_output_location(self._unload_location)
            try:
                dataset = parquet.ParquetDataset(
                    f"{bucket}/{key}", filesystem=self._fs, use_legacy_dataset=False
                )
                return to_column_info(dataset.schema)
            except Exception as e:
                _logger.exception(f"Failed to read schema {bucket}/{key}.")
                raise OperationalError(*e.args) from e
        elif engine == "fastparquet":
            from fastparquet import ParquetFile

            # TODO: https://github.com/python/mypy/issues/1153
            from pyathena.fastparquet.util import to_column_info  # type: ignore

            if not self._data_manifest:
                self._data_manifest = self._read_data_manifest()
            bucket, key = parse_output_location(self._data_manifest[0])
            try:
                file = ParquetFile(f"{bucket}/{key}", open_with=self._fs.open)
                return to_column_info(file.schema)
            except Exception as e:
                _logger.exception(f"Failed to read schema {bucket}/{key}.")
                raise OperationalError(*e.args) from e
        else:
            raise ProgrammingError("Engine must be one of `pyarrow`, `fastparquet`.")

    def _as_pandas(self) -> "DataFrame":
        if self.is_unload:
            engine = self.__get_engine()
            df = self._read_parquet(engine)
            if df.empty:
                self._metadata = tuple()
            else:
                self._metadata = self._read_parquet_schema(engine)
        else:
            df = self._read_csv()
        return df

    def as_pandas(self) -> "DataFrame":
        return self._df

    def close(self) -> None:
        import pandas as pd

        super(AthenaPandasResultSet, self).close()
        self._df = pd.DataFrame()
        self._iterrows = enumerate([])
        self._data_manifest = []
