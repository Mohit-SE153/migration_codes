CREATE TABLE [Purchasing].[ProductVendor] (
    [ProductID] int NOT NULL,
    [BusinessEntityID] int NOT NULL,
    [AverageLeadTime] int NOT NULL,
    [StandardPrice] money NOT NULL,
    [LastReceiptCost] money NULL,
    [LastReceiptDate] datetime NULL,
    [MinOrderQty] int NOT NULL,
    [MaxOrderQty] int NOT NULL,
    [OnOrderQty] int NULL,
    [UnitMeasureCode] nchar(3) NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
