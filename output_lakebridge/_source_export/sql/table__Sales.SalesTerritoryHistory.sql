CREATE TABLE [Sales].[SalesTerritoryHistory] (
    [BusinessEntityID] int NOT NULL,
    [TerritoryID] int NOT NULL,
    [StartDate] datetime NOT NULL,
    [EndDate] datetime NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
