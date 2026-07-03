CREATE TABLE [Sales].[SalesPersonQuotaHistory] (
    [BusinessEntityID] int NOT NULL,
    [QuotaDate] datetime NOT NULL,
    [SalesQuota] money NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
