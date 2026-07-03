CREATE TABLE [Sales].[SalesPerson] (
    [BusinessEntityID] int NOT NULL,
    [TerritoryID] int NULL,
    [SalesQuota] money NULL,
    [Bonus] money NOT NULL,
    [CommissionPct] smallmoney NOT NULL,
    [SalesYTD] money NOT NULL,
    [SalesLastYear] money NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
